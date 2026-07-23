"""Behavior tests for nix-bootstrap.sh.

Runs the real script via subprocess against throwaway /nix and /nix-baked
trees (``SEEKR_CHAIN_NIX_ROOT`` / ``SEEKR_CHAIN_NIX_BAKED_ROOT``). Every
assertion is about the script's observable filesystem/stdout/stderr/exit-code
behavior, never its internals.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources/nix-bootstrap.sh"


def _run_bootstrap(nix_root: Path, nix_baked: Path, extra_env: dict | None = None, timeout: int = 30):
    env = os.environ.copy()
    env["SEEKR_CHAIN_NIX_ROOT"] = str(nix_root)
    env["SEEKR_CHAIN_NIX_BAKED_ROOT"] = str(nix_baked)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def nix_root(tmp_path):
    root = tmp_path / "nix"
    root.mkdir()
    return root


@pytest.fixture
def nix_baked(tmp_path):
    baked = tmp_path / "nix-baked"
    baked.mkdir()
    (baked / "store").mkdir()
    (baked / "store" / "marker").write_text("toolchain-marker")
    return baked


class TestNixBootstrap:
    def test_bootstraps_from_nix_baked_when_done_marker_absent(self, nix_root, nix_baked):
        result = _run_bootstrap(nix_root, nix_baked)

        assert result.returncode == 0
        assert (nix_root / "store" / "marker").read_text() == "toolchain-marker"
        assert (nix_root / ".seekr-chain-bootstrap.done").exists()
        assert not (nix_root / ".seekr-chain-bootstrap.lock").exists()

    def test_skips_bootstrap_when_already_done(self, nix_root, nix_baked):
        (nix_root / ".seekr-chain-bootstrap.done").write_text("")

        result = _run_bootstrap(nix_root, nix_baked)

        assert result.returncode == 0
        assert not (nix_root / "store").exists()

    def test_fails_clearly_when_nix_baked_missing(self, nix_root, tmp_path):
        missing_baked = tmp_path / "does-not-exist"

        result = _run_bootstrap(nix_root, missing_baked)

        assert result.returncode == 1
        assert "not built with the" in result.stderr
        assert "seekr-nix-runner Dockerfile" in result.stderr

    def test_waits_for_concurrent_bootstrap_then_proceeds(self, nix_root, nix_baked):
        lock = nix_root / ".seekr-chain-bootstrap.lock"
        lock.mkdir()
        done = nix_root / ".seekr-chain-bootstrap.done"

        def finish_after_delay():
            time.sleep(1)
            done.write_text("")

        t = threading.Thread(target=finish_after_delay)
        t.start()
        try:
            result = _run_bootstrap(nix_root, nix_baked, timeout=15)
        finally:
            t.join()

        assert result.returncode == 0
        assert "another pod is bootstrapping" in result.stdout
        # Only the background helper wrote BOOTSTRAP_DONE -- this
        # invocation never ran do_bootstrap_copy itself.
        assert not (nix_root / "store").exists()

    def test_reclaims_stale_lock_after_timeout(self, nix_root, nix_baked):
        lock = nix_root / ".seekr-chain-bootstrap.lock"
        lock.mkdir()
        # BOOTSTRAP_DONE never appears -- simulates the original lock
        # holder having crashed before finishing.

        result = _run_bootstrap(
            nix_root,
            nix_baked,
            extra_env={"SEEKR_CHAIN_NIX_BOOTSTRAP_WAIT_S": "1"},
            timeout=15,
        )

        assert result.returncode == 0
        assert "reclaiming" in result.stderr
        assert (nix_root / "store" / "marker").read_text() == "toolchain-marker"
        assert (nix_root / ".seekr-chain-bootstrap.done").exists()
