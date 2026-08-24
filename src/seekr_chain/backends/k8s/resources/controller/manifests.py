"""JobSet manifest loading for the controller pod."""

import os

import yaml


def manifest_name(manifest: dict) -> str:
    return manifest["metadata"]["name"]


def load_manifest(assets_path: str, step_name: str) -> dict:
    path = os.path.join(assets_path, f"step={step_name}", "jobset.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def stamp_attempt(manifest: dict, js_name: str, attempt: int) -> None:
    """Mutate a freshly-loaded JobSet manifest dict in place for a retry
    resubmission: rename it to the new attempt's JobSet name, and update the
    seekr-chain/attempt annotation + label everywhere the original render put
    them (JobSet metadata, and every replicatedJob's pod template metadata) so
    failure.py's per-attempt pod label selector only picks up this attempt's
    pods."""
    manifest["metadata"]["name"] = js_name
    manifest["metadata"].setdefault("annotations", {})["seekr-chain/attempt"] = str(attempt)
    manifest["metadata"].setdefault("labels", {})["seekr-chain/attempt"] = str(attempt)
    for role in manifest["spec"]["replicatedJobs"]:
        pod_metadata = role["template"]["spec"]["template"]["metadata"]
        pod_metadata.setdefault("annotations", {})["seekr-chain/attempt"] = str(attempt)
        pod_metadata.setdefault("labels", {})["seekr-chain/attempt"] = str(attempt)
