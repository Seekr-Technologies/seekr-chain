#!/usr/bin/env python3
"""
DAG executor that runs inside the controller pod.

Reads pre-rendered JobSet manifests and dag.json from disk (downloaded from S3
by init containers) and submits them to Kubernetes in dependency order.

Required environment variables:
    SEEKR_CHAIN_JOB_ASSET_PATH        Path where assets were extracted (e.g. /seekr-chain/assets)
    SEEKR_CHAIN_NAMESPACE             Kubernetes namespace for JobSets
    SEEKR_CHAIN_CONTROLLER_JOB_NAME   Name of this controller Job (for ownerReferences)
    SEEKR_CHAIN_CONTROLLER_JOB_UID    UID of this controller Job (injected via downward API
                                      from batch.kubernetes.io/controller-uid pod label)

Only depends on: Python stdlib + kubernetes + pyyaml
"""

import json
import os
import sys
import time

import kubernetes
import yaml

POLL_INTERVAL = int(os.environ.get("SEEKR_CHAIN_POLL_INTERVAL", "5"))


def _manifest_name(manifest: dict) -> str:
    return manifest["metadata"]["name"]


def _load_manifest(assets_path: str, step_name: str) -> dict:
    path = os.path.join(assets_path, f"step={step_name}", "jobset.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _get_terminal_state(k8s_custom, js_name: str, namespace: str) -> str | None:
    """Return the JobSet's terminalState ('Completed' or 'Failed'), or None if not terminal."""
    try:
        js = k8s_custom.get_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            name=js_name,
        )
        return js.get("status", {}).get("terminalState") or None
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise


def _cascade_fail(dag: list[dict], phases: dict[str, str]) -> None:
    """Mark PENDING steps whose dependencies (transitively) include a failed step."""
    changed = True
    while changed:
        changed = False
        for step in dag:
            name = step["name"]
            deps = step.get("depends_on") or []
            if phases[name] == "PENDING" and any(phases[d] == "FAILED" for d in deps):
                phases[name] = "FAILED"
                print(f"[controller] step={name!r} cascade-failed", flush=True)
                changed = True


def main() -> int:
    assets_path = os.environ["SEEKR_CHAIN_JOB_ASSET_PATH"]
    namespace = os.environ["SEEKR_CHAIN_NAMESPACE"]
    job_name = os.environ["SEEKR_CHAIN_CONTROLLER_JOB_NAME"]
    job_uid = os.environ["SEEKR_CHAIN_CONTROLLER_JOB_UID"]

    kubernetes.config.load_incluster_config()
    k8s_custom = kubernetes.client.CustomObjectsApi()

    # Load DAG definition from assets
    with open(os.path.join(assets_path, "dag.json")) as f:
        dag = json.load(f)  # [{"name": "a", "depends_on": ["b", ...]}, ...]

    print(f"[controller] loaded DAG with {len(dag)} steps: {[s['name'] for s in dag]}", flush=True)

    # ownerReference so JobSets are cascade-deleted when this controller Job is deleted.
    # job_uid comes from the downward API (batch.kubernetes.io/controller-uid label) —
    # no API call or extra RBAC required.
    owner_ref = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": job_name,
            "uid": job_uid,
            "blockOwnerDeletion": True,
            "controller": True,
        }
    ]

    phases: dict[str, str] = {s["name"]: "PENDING" for s in dag}
    # step_name -> jobset manifest name (populated after submission)
    js_names: dict[str, str] = {}

    while True:
        for step in dag:
            name = step["name"]
            deps = step.get("depends_on") or []

            if phases[name] == "PENDING":
                if all(phases[d] == "SUCCEEDED" for d in deps):
                    manifest = _load_manifest(assets_path, name)
                    manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
                    js_name = _manifest_name(manifest)
                    js_names[name] = js_name
                    k8s_custom.create_namespaced_custom_object(
                        group="jobset.x-k8s.io",
                        version="v1alpha2",
                        plural="jobsets",
                        namespace=namespace,
                        body=manifest,
                    )
                    phases[name] = "SUBMITTED"
                    print(f"[controller] submitted step={name!r} jobset={js_name!r}", flush=True)

            elif phases[name] in ("SUBMITTED", "RUNNING"):
                js_name = js_names[name]
                terminal = _get_terminal_state(k8s_custom, js_name, namespace)
                if terminal == "Completed":
                    phases[name] = "SUCCEEDED"
                    print(f"[controller] step={name!r} SUCCEEDED", flush=True)
                elif terminal == "Failed":
                    phases[name] = "FAILED"
                    print(f"[controller] step={name!r} FAILED", flush=True)
                elif phases[name] == "SUBMITTED":
                    phases[name] = "RUNNING"

        _cascade_fail(dag, phases)

        if all(p in ("SUCCEEDED", "FAILED") for p in phases.values()):
            break

        time.sleep(POLL_INTERVAL)

    failed = [n for n, p in phases.items() if p == "FAILED"]
    if failed:
        print(f"[controller] workflow FAILED — failed steps: {failed}", file=sys.stderr, flush=True)
        return 1

    print("[controller] workflow SUCCEEDED — all steps completed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
