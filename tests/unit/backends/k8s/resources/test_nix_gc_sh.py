"""Behavior tests for nix-gc.sh.

Runs the real script via subprocess against a throwaway store root
(``SEEKR_CHAIN_NIX_ROOT``) and a curated ``PATH`` — a fake ``nix`` binary
substituted for the real one, plus a symlink farm of the handful of real
system binaries the script needs (``du``, ``awk``, ``mkdir``, ``ln``, and
optionally ``flock``). This is dependency injection at the process boundary,
not internals testing: every assertion is about the script's observable
filesystem/stdout/exit-code behavior given a filesystem state and env vars.
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources/nix-gc.sh"

_REAL_BINS = ["sh", "du", "awk", "mkdir", "ln", "cat", "rm", "date", "expr", "env"]


def _make_real_bin_dir(tmp_path: Path, name: str, include_flock: bool) -> Path:
    bindir = tmp_path / name
    bindir.mkdir()
    names = list(_REAL_BINS) + (["flock"] if include_flock else [])
    for binname in names:
        real = shutil.which(binname)
        if real:
            (bindir / binname).symlink_to(real)
    return bindir


def _make_fake_nix(tmp_path: Path, sentinel: Path, exit_code: int = 0) -> Path:
    bindir = tmp_path / "fake-nix-bin"
    bindir.mkdir()
    nix = bindir / "nix"
    nix.write_text(
        "#!/bin/sh\n"
        f'if [ "$1 $2" = "store gc" ]; then\n'
        f'  echo "GC_CALLED $*" >> "{sentinel}"\n'
        f"  exit {exit_code}\n"
        "fi\n"
        'echo "unexpected nix invocation: $*" >&2\n'
        "exit 1\n"
    )
    nix.chmod(nix.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run_gc(nix_root: Path, path_dirs: list, extra_env: dict) -> subprocess.CompletedProcess:
    env = {
        "PATH": ":".join(str(p) for p in path_dirs),
        "SEEKR_CHAIN_NIX_ROOT": str(nix_root),
        "SEEKR_CHAIN_NIX_CLOSURE": "/nix/store/abc123-hello",
    }
    env.update(extra_env)
    return subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def nix_root(tmp_path):
    root = tmp_path / "nix"
    root.mkdir()
    (root / "some-store-path").write_bytes(b"x" * 4096)
    return root


class TestNixGc:
    def test_skips_gc_when_under_budget(self, tmp_path, nix_root):
        sentinel = tmp_path / "gc-called.log"
        fake_nix_bin = _make_fake_nix(tmp_path, sentinel)
        real_bin = _make_real_bin_dir(tmp_path, "real-bin", include_flock=True)

        result = _run_gc(
            nix_root,
            [fake_nix_bin, real_bin],
            {
                "SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES": "1000",
                "SEEKR_CHAIN_NIX_STORE_MAX_BYTES": "2000",
            },
        )

        assert result.returncode == 0
        assert "no GC needed" in result.stdout
        assert not sentinel.exists()

    def test_runs_gc_when_over_budget(self, tmp_path, nix_root):
        sentinel = tmp_path / "gc-called.log"
        fake_nix_bin = _make_fake_nix(tmp_path, sentinel)
        real_bin = _make_real_bin_dir(tmp_path, "real-bin", include_flock=True)

        result = _run_gc(
            nix_root,
            [fake_nix_bin, real_bin],
            {
                "SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES": "1000000000",
                "SEEKR_CHAIN_NIX_STORE_MAX_BYTES": "1000",
            },
        )

        assert result.returncode == 0
        assert sentinel.exists()
        assert "--max" in sentinel.read_text()
        gcroot = nix_root / "var/nix/gcroots/seekr-chain/active"
        assert gcroot.is_symlink()
        assert str(gcroot.resolve()) == "/nix/store/abc123-hello"
        assert "freed" in result.stdout

    def test_gc_failure_does_not_fail_pod(self, tmp_path, nix_root):
        sentinel = tmp_path / "gc-called.log"
        fake_nix_bin = _make_fake_nix(tmp_path, sentinel, exit_code=1)
        real_bin = _make_real_bin_dir(tmp_path, "real-bin", include_flock=True)

        result = _run_gc(
            nix_root,
            [fake_nix_bin, real_bin],
            {
                "SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES": "1000000000",
                "SEEKR_CHAIN_NIX_STORE_MAX_BYTES": "1000",
            },
        )

        assert result.returncode == 0
        assert sentinel.exists()

    def test_proceeds_without_serialization_when_flock_missing(self, tmp_path, nix_root):
        sentinel = tmp_path / "gc-called.log"
        fake_nix_bin = _make_fake_nix(tmp_path, sentinel)
        real_bin = _make_real_bin_dir(tmp_path, "real-bin", include_flock=False)

        result = _run_gc(
            nix_root,
            [fake_nix_bin, real_bin],
            {
                "SEEKR_CHAIN_NIX_STORE_CURRENT_BYTES": "1000000000",
                "SEEKR_CHAIN_NIX_STORE_MAX_BYTES": "1000",
            },
        )

        assert result.returncode == 0
        assert "flock not available" in result.stderr
        assert sentinel.exists()
