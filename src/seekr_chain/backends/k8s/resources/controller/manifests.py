"""JobSet manifest loading for the controller pod."""

import os

import yaml


def manifest_name(manifest: dict) -> str:
    return manifest["metadata"]["name"]


def load_manifest(assets_path: str, step_name: str) -> dict:
    path = os.path.join(assets_path, f"step={step_name}", "jobset.yaml")
    with open(path) as f:
        return yaml.safe_load(f)
