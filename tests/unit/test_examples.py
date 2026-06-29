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


@pytest.mark.parametrize("dir_path", dir_paths)
class TestExamples:
    def test_config_parses(self, dir_path: Path):
        """Config YAML is valid and matches the WorkflowConfig schema."""
        config = WorkflowConfig.model_validate(yaml.safe_load((dir_path / "config.yaml").read_text()))
        assert len(config.steps) > 0

    def test_renders(self, dir_path: Path, tmp_path):
        """Each step renders to valid Argo JobSet YAML."""
        config = WorkflowConfig.model_validate(yaml.safe_load((dir_path / "config.yaml").read_text()))
        job_info = get_job_info("test-abc1", datastore_root=_DATASTORE_ROOT)

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
