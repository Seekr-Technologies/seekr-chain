#!/usr/bin/env python3

import subprocess
from pathlib import Path

import pytest
import yaml

# Collect all example dirs
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "examples"
dir_paths = [pytest.param(p, id=p.name) for p in sorted(BASE_DIR.iterdir()) if p.is_dir()]


def _example_needs_gpu(dir_path: Path) -> bool:
    """Return True if any step in the example config requests GPUs."""
    config_file = dir_path / "config.yaml"
    with open(config_file) as f:
        cfg = yaml.safe_load(f)
    return any((step.get("resources") or {}).get("gpus_per_node", 0) or 0 for step in cfg.get("steps", []))


@pytest.mark.parametrize("dir_path", dir_paths)
class TestExamples:
    def test_run(self, dir_path: Path, hermetic_flag):
        if hermetic_flag and _example_needs_gpu(dir_path):
            pytest.skip(f"Example {dir_path.name} requires GPU, skipping in hermetic mode")

        result = subprocess.run(["chain", "submit", "config.yaml", "--follow"], cwd=dir_path)

        assert result.returncode == 0
