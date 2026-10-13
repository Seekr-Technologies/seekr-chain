"""Behavior tests for failed-build Nix output publication."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
SCRIPT = RESOURCES / "nix-build.sh"
HOOK = RESOURCES / "nix-post-build-hook.sh"
CLOSURE = "/nix/store/cccccccccccccccccccccccccccccccc-final"

_REAL_BINS = ["awk", "cat", "date", "grep", "head", "mkdir", "mkfifo", "rm", "tee", "wc"]


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in _REAL_BINS:
        (bindir / name).symlink_to(shutil.which(name))

    _write_exe(
        bindir / "sh",
        """#!/bin/sh
if [ "$1" = /seekr-chain/resources/nix-bootstrap.sh ]; then
    exit 0
fi
exec /bin/sh "$@"
""",
    )
    _write_exe(
        bindir / "nix",
        """#!/bin/sh
cmd=$1
shift
case "$cmd" in
  build)
    printf '%s\\n' "$NIX_CONFIG" > "$FAKE_NIX_CONFIG_LOG"
    OUT_PATHS="$FAKE_OUT_PATHS" "$FAKE_HOOK"
    if [ "$FAKE_BUILD_STATUS" -eq 0 ]; then
        echo "$FAKE_CLOSURE"
    fi
    exit "$FAKE_BUILD_STATUS"
    ;;
  path-info)
    case "$1" in
      --recursive)
        echo "$FAKE_CLOSURE"
        exit 0
        ;;
      --closure-size)
        printf '%s\\t4096\\n' "$FAKE_CLOSURE"
        exit 0
        ;;
      "$FAKE_INVALID_PATH")
        exit 1
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  copy)
    count=0
    paths=
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --to ]; then
            shift 2
            continue
        fi
        count=$((count + 1))
        paths="$paths $1"
        shift
    done
    call=1
    if [ -f "$FAKE_COPY_COUNT" ]; then
        call=$(( $(cat "$FAKE_COPY_COUNT") + 1 ))
    fi
    echo "$call" > "$FAKE_COPY_COUNT"
    printf '%s:%s:%s\\n' "$call" "$count" "$paths" >> "$FAKE_COPY_LOG"
    if [ "${FAKE_COPY_FAIL_CALL:-}" = "$call" ]; then
        echo "simulated copy failure" >&2
        exit 19
    fi
    echo "uploaded 's3://test' (10 bytes)" >&2
    exit 0
    ;;
esac
exit 2
""",
    )
    return bindir


def _store_path(i: int) -> str:
    return f"/nix/store/{i:032d}-output-{i}"


def _run_build(
    tmp_path: Path,
    *,
    build_status: int,
    outputs: list[str],
    invalid_path: str = "/no/invalid/path",
    copy_fail_call: int | None = None,
    nix_config: str = "",
):
    bindir = _fake_bin(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    copy_log = tmp_path / "copies"
    copy_count = tmp_path / "copy-count"
    config_log = tmp_path / "nix-config"

    env = {
        "PATH": f"{bindir}:/bin:/usr/bin",
        "SEEKR_CHAIN_NIX_STORE": "s3://test",
        "SEEKR_CHAIN_NIX_CLOSURE": CLOSURE,
        "SEEKR_CHAIN_NIX_WORKSPACE": str(workspace),
        "SEEKR_CHAIN_NIX_EXPRESSION": ".",
        "SEEKR_CHAIN_NIX_SYSTEM": "x86_64-linux",
        "SEEKR_CHAIN_NIX_ATTR": "default",
        "SEEKR_CHAIN_NIX_COMPRESSION": "zstd",
        "SEEKR_CHAIN_NIX_MAX_JOBS": "1",
        "SEEKR_CHAIN_NIX_CORES": "0",
        "FAKE_BUILD_STATUS": str(build_status),
        "FAKE_OUT_PATHS": " ".join(outputs),
        "FAKE_INVALID_PATH": invalid_path,
        "FAKE_CLOSURE": CLOSURE,
        "FAKE_HOOK": str(HOOK),
        "FAKE_COPY_LOG": str(copy_log),
        "FAKE_COPY_COUNT": str(copy_count),
        "FAKE_NIX_CONFIG_LOG": str(config_log),
        "NIX_CONFIG": nix_config,
    }
    if copy_fail_call is not None:
        env["FAKE_COPY_FAIL_CALL"] = str(copy_fail_call)

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    copies = copy_log.read_text().splitlines() if copy_log.exists() else []
    config = config_log.read_text() if config_log.exists() else ""
    return result, copies, config


class TestPostBuildHook:
    def test_concurrent_invocations_create_atomic_unique_entries(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        outputs = [_store_path(1), _store_path(2)]
        env = {**os.environ, "SEEKR_CHAIN_NIX_BUILD_JOURNAL": str(journal), "OUT_PATHS": " ".join(outputs)}

        processes = [subprocess.Popen([str(HOOK)], env=env) for _ in range(8)]
        assert [p.wait(timeout=10) for p in processes] == [0] * 8
        assert sorted(p.name for p in journal.iterdir()) == sorted(Path(p).name for p in outputs)
        assert all(p.is_dir() for p in journal.iterdir())

    def test_ignores_paths_outside_the_nix_store(self, tmp_path):
        journal = tmp_path / "journal"
        journal.mkdir()
        result = subprocess.run(
            [str(HOOK)],
            env={
                **os.environ,
                "SEEKR_CHAIN_NIX_BUILD_JOURNAL": str(journal),
                "OUT_PATHS": "/tmp/not-store /nix/store/name/with/slash",
            },
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert list(journal.iterdir()) == []
        assert "ignoring non-store output path" in result.stderr
        assert "ignoring malformed output path" in result.stderr


class TestNixBuildPartialUpload:
    def test_failed_build_validates_and_uploads_in_bounded_batches(self, tmp_path):
        valid = [_store_path(i) for i in range(101)]
        invalid = _store_path(999)
        result, copies, config = _run_build(
            tmp_path,
            build_status=42,
            outputs=valid + [invalid],
            invalid_path=invalid,
        )

        assert result.returncode == 42
        assert [line.split(":", 2)[1] for line in copies] == ["100", "1"]
        assert invalid not in "\\n".join(copies)
        assert "partial cache upload complete: 101 paths copied" in result.stdout
        assert "skipped 1 invalid path" in result.stderr
        assert "post-build-hook = /seekr-chain/resources/nix-post-build-hook.sh" in config

    def test_partial_copy_error_is_visible_but_original_status_wins(self, tmp_path):
        result, copies, _ = _run_build(
            tmp_path,
            build_status=37,
            outputs=[_store_path(1)],
            copy_fail_call=1,
        )

        assert len(copies) == 1
        assert result.returncode == 37
        assert "simulated copy failure" in result.stderr
        assert "partial cache upload batch failed with status 19" in result.stderr

    @pytest.mark.parametrize("status", [129, 130, 137, 143])
    def test_signal_status_skips_partial_upload(self, tmp_path, status):
        result, copies, _ = _run_build(
            tmp_path,
            build_status=status,
            outputs=[_store_path(1)],
        )

        assert result.returncode == status
        assert copies == []
        assert "skipping partial cache upload" in result.stderr

    def test_success_keeps_the_existing_full_closure_copy(self, tmp_path):
        result, copies, _ = _run_build(
            tmp_path,
            build_status=0,
            outputs=[CLOSURE],
        )

        assert result.returncode == 0, result.stderr
        assert len(copies) == 1
        _, count, paths = copies[0].split(":", 2)
        assert count == "1"
        assert paths.strip() == CLOSURE
        assert "partial cache upload" not in result.stdout
        assert f"pushed {CLOSURE} to s3://test" in result.stdout

    def test_existing_nix_config_is_preserved(self, tmp_path):
        result, _, config = _run_build(
            tmp_path,
            build_status=1,
            outputs=[],
            nix_config="extra-experimental-features = ca-derivations",
        )

        assert result.returncode == 1
        assert "extra-experimental-features = ca-derivations" in config
        assert "post-build-hook = /seekr-chain/resources/nix-post-build-hook.sh" in config
