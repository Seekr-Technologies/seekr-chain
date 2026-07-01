"""Integration tests for nix-mode workflows.

``test_build_then_warm`` — closure missing → in-cluster build + s3 push,
then a second submit hits the cache and skips the build step. Verifies
the cache-hit contract end-to-end against the hermetic minio store.

Needs ``nix`` locally to evaluate the closure hash at submit time; we
skip when nix is absent.
"""

import platform
import shutil

import pytest

import seekr_chain
from seekr_chain._testing import assert_nested_match

_NIX_AVAILABLE = shutil.which("nix") is not None
# nix system strings match `uname -m` on linux (x86_64, aarch64).
_NIX_SYSTEM = f"{platform.machine()}-linux"


@pytest.fixture
def nix_basic_dir(monkeypatch, test_code_dir):
    """Chdir into the nix-basic flake directory for the test.

    ``nix.expression: "./"`` is interpreted both on the test runner (for
    submit-time eval) and inside the build pod (``cd /seekr-chain/workspace
    && nix build path:./``). Chdir keeps both sides consistent without
    requiring an absolute path that would fail inside the pod.
    """
    d = test_code_dir / "7_nix_basic"
    monkeypatch.chdir(d)
    return d


def _evict_narinfo(s3_client, bucket: str, closure_path: str) -> None:
    """Delete the closure's narinfo from the bucket so it looks "missing".

    The hermetic minio container persists locally across sessions, so a
    previous run may have populated the cache. We delete the narinfo (the
    one file ``closure_exists`` looks for); the nar blobs are content-
    addressed and reuploading them is a no-op when present.
    """
    from seekr_chain.nix_utils import closure_hash_from_path

    hash_ = closure_hash_from_path(closure_path)
    try:
        s3_client.delete_object(Bucket=bucket, Key=f"{hash_}.narinfo")
    except Exception:
        # Bucket missing, object missing — either way, downstream
        # closure_exists will see no narinfo, which is what we want.
        pass


class TestNixMode:
    @pytest.mark.skipif(not _NIX_AVAILABLE, reason="requires nix on the test runner for submit-time eval")
    def test_build_then_warm(self, nix_basic_dir, s3_client):
        """Two submits of the same closure: first builds, second cache-hits.

        Expected log shape:
        - First job: ``step=nix-build-<hash>`` (synthesized) + ``step=step``
        - Second job: only ``step=step``
        """
        from seekr_chain import nix_utils

        # Pre-resolve the closure hash so we can evict any stale narinfo
        # from a previous run. Without this, "first run builds" is flaky
        # when minio persists across sessions.
        closure = nix_utils.eval_closure_path("./", system=_NIX_SYSTEM)
        _evict_narinfo(s3_client, "seekr-chain-test", closure)

        def make_config():
            return seekr_chain.WorkflowConfig.model_validate(
                {
                    "name": "test-nix",  # overridden by patch_configs_for_testing
                    "namespace": "argo-workflows",
                    "code": {"path": "."},
                    "steps": [
                        {
                            "name": "step",
                            "nix": {
                                "expression": "./",
                                "system": _NIX_SYSTEM,
                                "store": "s3://seekr-chain-test",
                                "build": True,
                                # Minimal resources so the build pod fits on hermetic
                                # k3d. pkgs.hello is a few-MB closure that builds in
                                # seconds; the default 4cpu/16Gi is overkill here.
                                "build_resources": {
                                    "num_nodes": 1,
                                    "cpus_per_node": "250m",
                                    "mem_per_node": "512Mi",
                                    "ephemeral_storage_per_node": "2Gi",
                                },
                            },
                            "script": "hello",
                        }
                    ],
                }
            )

        # ---------- first submit: closure missing → build step injected ----------
        job1 = seekr_chain.launch_argo_workflow(make_config())
        job1.follow()
        status1 = seekr_chain.wait(job1, poll_interval=1)
        assert status1.is_successful(), f"first submit did not succeed: {status1}"

        logs1 = job1.get_logs().to_dict()
        build_keys_1 = [k for k in logs1 if k.startswith("step=nix-build-")]
        assert len(build_keys_1) == 1, f"expected exactly one nix-build- step in first run, got: {sorted(logs1)}"
        # User step ran and the closure's hello executed via PATH=$CLOSURE/bin
        assert_nested_match(
            logs1["step=step"],
            {
                "index=0": {"attempt=0": ["Hello, world!", ""]},
            },
        )

        # ---------- second submit: closure present → no build step ----------
        job2 = seekr_chain.launch_argo_workflow(make_config())
        job2.follow()
        status2 = seekr_chain.wait(job2, poll_interval=1)
        assert status2.is_successful(), f"second submit did not succeed: {status2}"

        logs2 = job2.get_logs().to_dict()
        build_keys_2 = [k for k in logs2 if k.startswith("step=nix-build-")]
        assert build_keys_2 == [], (
            f"expected no nix-build- step in second run (closure should be in cache); got: {sorted(logs2)}"
        )
        assert_nested_match(
            logs2["step=step"],
            {
                "index=0": {"attempt=0": ["Hello, world!", ""]},
            },
        )
