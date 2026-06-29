#!/usr/bin/env python3
"""Validate that all example configs parse correctly and render to valid YAML."""

from pathlib import Path

import pytest
import yaml

from seekr_chain.backends.k8s.job_info import get_job_info
from seekr_chain.backends.k8s.jobset import create_jobset_manifest
from seekr_chain.config import WorkflowConfig

BASE_DIR = Path(__file__).resolve().parent.parent.parent / "examples"
dir_paths = [pytest.param(p, id=p.name) for p in sorted(BASE_DIR.iterdir()) if p.is_dir()]

_DATASTORE_ROOT = "s3://test-bucket/seekr-chain/"


def _config_uses_nix(config) -> bool:
    """Return True if any role in any step uses the nix-mode runtime."""
    for step in config.steps:
        # SingleRoleStepConfig has .nix directly; MultiRoleStepConfig has .roles
        roles = step.roles if hasattr(step, "roles") else [step]
        if any(getattr(r, "nix", None) is not None for r in roles):
            return True
    return False


@pytest.mark.parametrize("dir_path", dir_paths)
class TestExamples:
    def test_config_parses(self, dir_path: Path):
        """Config YAML is valid and matches the WorkflowConfig schema."""
        config = WorkflowConfig.model_validate(yaml.safe_load((dir_path / "config.yaml").read_text()))
        assert len(config.steps) > 0

    def test_renders(self, dir_path: Path, tmp_path, monkeypatch):
        """Each step renders to valid Argo JobSet YAML."""
        config = WorkflowConfig.model_validate(yaml.safe_load((dir_path / "config.yaml").read_text()))
        job_info = get_job_info("test-abc1", datastore_root=_DATASTORE_ROOT)

        # If any step uses nix-mode, stub out the submit-time helpers so this
        # test can run without `nix` on PATH and without real S3 access. The
        # individual eval/exists/render branches are covered directly in
        # test_nix_utils.py and test_nix_role.py; here we just want the
        # rendering pipeline to flow end-to-end on the example config.
        if _config_uses_nix(config):
            from seekr_chain.backends.k8s import jobset as jobset_mod
            from seekr_chain.user_config import UserConfig

            monkeypatch.setattr(
                jobset_mod, "_user_config",
                UserConfig(
                    nix_store="s3://fake-test-bucket/cache",
                    nix_runner_image="registry.example.com/nix-runner:test",
                ),
            )
            monkeypatch.setattr(
                "seekr_chain.nix_utils.eval_closure_path",
                lambda *_a, **_k: "/nix/store/0000000000000000000000000000000000-test-closure",
            )
            monkeypatch.setattr(
                "seekr_chain.nix_utils.closure_exists",
                lambda *_a, **_k: True,
            )

        for step_index in range(len(config.steps)):
            js_name, js_yaml = create_jobset_manifest(
                workflow_config=config,
                step_index=step_index,
                job_info=job_info,
                workflow_name="test-abc1",
                workflow_secrets=[],
                interactive=False,
                assets_path=tmp_path / "assets",
            )
            parsed = yaml.safe_load(js_yaml)
            assert parsed["apiVersion"] == "jobset.x-k8s.io/v1alpha2"
            assert parsed["kind"] == "JobSet"
            assert parsed["metadata"]["name"] == js_name
