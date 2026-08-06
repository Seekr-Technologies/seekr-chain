"""Behavior tests for chain-nix-init.sh.

Runs the real script via subprocess against a throwaway store root
(``SEEKR_CHAIN_NIX_ROOT``), nix.conf (``SEEKR_CHAIN_NIX_CONF``) and resource
dir (``SEEKR_CHAIN_RESOURCE_DIR``), on a curated ``PATH`` with fake ``nix``
and (where the test needs it) fake ``du`` binaries. Same dependency-injection-
at-the-process-boundary approach as ``test_nix_gc_sh.py``: every assertion is
about exit code and stdout/stderr given a filesystem state and env vars.

The centre of gravity here is the incident these tests exist to prevent. This
script used to run under ``set -euo pipefail`` while computing purely cosmetic
summary numbers via ``$(du -sk /nix 2>/dev/null | awk ...)``. Because ``awk``
always succeeds, ``pipefail`` was the sole thing propagating a ``du`` failure,
and ``2>/dev/null`` discarded the reason -- so a transient non-zero ``du``
(routine on a hostPath ``/nix`` that peer pods are mutating) killed the init
container with exit 1 and an empty log. Five pods died that way on one node.

``test_failing_du_does_not_fail_the_pod`` is the regression test for exactly
that; the rest fence off the same shape elsewhere in the script.
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
SCRIPT = RESOURCES / "chain-nix-init.sh"

CLOSURE = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-test-env"

# Everything the script (and the nix-bootstrap.sh / nix-gc.sh it shells out to)
# actually invokes. `du` is deliberately NOT here: each test decides whether to
# inject a real or a failing one.
_REAL_BINS = [
    "sh",
    "awk",
    "cat",
    "cp",
    "date",
    "expr",
    "grep",
    "head",
    "kill",
    "ln",
    "mkdir",
    "mkfifo",
    "rm",
    "rmdir",
    "sed",
    "sleep",
    "tee",
    "touch",
    "wc",
]


def _bin_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _make_real_bin_dir(tmp_path: Path, *, real_du: bool) -> Path:
    bindir = _bin_dir(tmp_path, "real-bin")
    for binname in _REAL_BINS + (["du"] if real_du else []):
        real = shutil.which(binname)
        if real:
            (bindir / binname).symlink_to(real)
    return bindir


def _write_exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _make_failing_du(tmp_path: Path) -> Path:
    """A ``du`` that always fails, the way a real one does mid-walk when a peer
    pod renames or deletes an entry out from under it."""
    bindir = _bin_dir(tmp_path, "failing-du-bin")
    _write_exe(
        bindir / "du",
        "#!/bin/sh\necho \"du: cannot access '/nix/store/x': No such file or directory\" >&2\nexit 1\n",
    )
    return bindir


def _make_fake_nix(
    tmp_path: Path,
    *,
    path_info_exit: int = 0,
    copy_exit: int = 0,
    name: str = "fake-nix-bin",
) -> Path:
    """Fake ``nix``. ``path-info`` exit 0 means "closure fully present", which
    sends the script down its fast path; non-zero means a real pull is needed
    (and, in the summary block, that the stats are unavailable)."""
    bindir = _bin_dir(tmp_path, name)
    _write_exe(
        bindir / "nix",
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        f'  "path-info --recursive") echo "{CLOSURE}"; exit {path_info_exit};;\n'
        f'  "path-info --closure-size") echo "{CLOSURE}	4096"; exit {path_info_exit};;\n'
        "esac\n"
        'case "$1" in\n'
        f"  copy) exit {copy_exit};;\n"
        "esac\n"
        "exit 0\n",
    )
    return bindir


def _run_init(tmp_path: Path, path_dirs: list, extra_env: dict = None):
    nix_root = tmp_path / "nix"
    nix_root.mkdir(exist_ok=True)
    baked = tmp_path / "nix-baked"
    (baked / "store").mkdir(parents=True, exist_ok=True)
    (baked / "var").mkdir(exist_ok=True)
    nix_conf = tmp_path / "nix.conf"
    nix_conf.touch()

    env = {
        "PATH": ":".join(str(p) for p in path_dirs),
        "SEEKR_CHAIN_NIX_ROOT": str(nix_root),
        "SEEKR_CHAIN_NIX_BAKED_ROOT": str(baked),
        "SEEKR_CHAIN_NIX_CONF": str(nix_conf),
        "SEEKR_CHAIN_RESOURCE_DIR": str(RESOURCES),
        "SEEKR_CHAIN_NIX_STORE": "oci://namespace/bucket/prefix",
        "SEEKR_CHAIN_NIX_CLOSURE": CLOSURE,
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestChainNixInit:
    def test_succeeds_on_the_happy_path(self, tmp_path):
        result = _run_init(
            tmp_path,
            [_make_fake_nix(tmp_path), _make_real_bin_dir(tmp_path, real_du=True)],
        )

        assert result.returncode == 0, result.stderr
        assert "chain-nix-init summary" in result.stdout
        assert "total wall time" in result.stdout

    def test_failing_du_does_not_fail_the_pod(self, tmp_path):
        """The incident, reproduced. ``du`` is used only to print a number in
        the summary banner -- its exit status says nothing about whether the
        closure is usable, so it must never abort the container."""
        result = _run_init(
            tmp_path,
            [
                _make_failing_du(tmp_path),
                _make_fake_nix(tmp_path),
                _make_real_bin_dir(tmp_path, real_du=False),
            ],
        )

        assert result.returncode == 0, result.stderr
        assert "chain-nix-init summary" in result.stdout
        assert "total wall time" in result.stdout

    def test_failing_path_info_does_not_fail_the_pod_but_warns(self, tmp_path):
        """Unlike ``du``, a failing ``nix path-info`` after a successful pull
        can mean the closure isn't fully registered. Tolerated -- the pod's
        workload should still run -- but never silent."""
        result = _run_init(
            tmp_path,
            [
                _make_fake_nix(tmp_path, path_info_exit=1),
                _make_real_bin_dir(tmp_path, real_du=True),
            ],
        )

        assert result.returncode == 0, result.stderr
        assert "chain-nix-init summary" in result.stdout
        assert "warning: 'nix path-info" in result.stderr
        assert "not fully registered" in result.stderr

    def test_genuine_pull_failure_still_fails_and_names_the_stage(self, tmp_path):
        """The flip side: hardening the cosmetic stats must not have made real
        failures survivable. And when one happens, the trap must say where --
        the whole reason the original incident took so long to diagnose is that
        exit 1 arrived with an empty log."""
        result = _run_init(
            tmp_path,
            [
                _make_fake_nix(tmp_path, path_info_exit=1, copy_exit=1),
                _make_real_bin_dir(tmp_path, real_du=True),
            ],
        )

        assert result.returncode == 1
        assert "failed after 3 attempts" in result.stdout
        assert "chain-nix-init: FAILED (exit 1) during stage: closure pull" in result.stderr

    def test_du_stderr_is_preserved_not_discarded(self, tmp_path):
        """``2>/dev/null`` on the original ``du`` is half of why the incident
        was undiagnosable. The reason has to land somewhere recoverable."""
        log = tmp_path / "nix-init.log"
        result = _run_init(
            tmp_path,
            [
                _make_failing_du(tmp_path),
                _make_fake_nix(tmp_path),
                _make_real_bin_dir(tmp_path, real_du=False),
            ],
            {"TMPDIR": str(tmp_path)},
        )

        assert result.returncode == 0, result.stderr
        # $LOG is /tmp/nix-init.log inside the container; in-test it's the same
        # absolute path, so read whichever exists.
        for candidate in (log, Path("/tmp/nix-init.log")):
            if candidate.exists() and "du: cannot access" in candidate.read_text():
                break
        else:
            pytest.fail("du's stderr was not captured to the init log")


class TestNoPipefail:
    def test_script_does_not_set_pipefail(self):
        """A guard on the specific footgun, not just its current instances. Any
        future ``cmd | awk`` added to this script is safe by default only for
        as long as pipefail stays off."""
        assert "pipefail" not in _uncommented(SCRIPT)

    @pytest.mark.parametrize("script", ["nix-gc.sh", "nix-bootstrap.sh", "nix-build.sh"])
    def test_sibling_scripts_do_not_set_pipefail(self, script):
        assert "pipefail" not in _uncommented(RESOURCES / script)


def _uncommented(path: Path) -> str:
    """Script text with comment lines stripped -- these scripts discuss
    pipefail at length in comments, and those must not trip the guard."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
