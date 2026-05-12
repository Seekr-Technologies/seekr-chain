#!/usr/bin/env python3
"""
DAG executor that runs inside the controller pod.

Reads pre-rendered JobSet manifests and dag.json from disk (downloaded from S3
by init containers) and submits them to Kubernetes in dependency order.

Uses the Kubernetes watch API to react immediately when a JobSet reaches a
terminal state, rather than polling on a fixed interval. The watch stream
reconnects automatically on transient errors, resuming from the last seen
resourceVersion so no events are missed.

Reliability features:
  - Phase state is persisted to a ConfigMap after every transition, so a
    restarted controller pod can resume exactly where it left off (rather than
    re-inferring state from 409 Conflict responses alone).
  - Step transitions are emitted as Kubernetes Events, visible via
    ``kubectl describe job <workflow-id>``.
  - A heartbeat file (``/tmp/controller-heartbeat``) is touched at startup and
    after every watch stream iteration.  The Job spec mounts a liveness probe
    that kills the container if the heartbeat goes stale, triggering a pod
    restart and watch-stream reconnect.

Required environment variables:
    SEEKR_CHAIN_JOB_ASSET_PATH        Path where assets were extracted (e.g. /seekr-chain/assets)
    SEEKR_CHAIN_NAMESPACE             Kubernetes namespace for JobSets
    SEEKR_CHAIN_CONTROLLER_JOB_NAME   Name of this controller Job (for ownerReferences)
    SEEKR_CHAIN_CONTROLLER_JOB_UID    UID of this controller Job (injected via downward API
                                      from batch.kubernetes.io/controller-uid pod label)

Only depends on: Python stdlib + kubernetes + pyyaml
"""

import datetime
import json
import os
import sys
import time

import kubernetes
import kubernetes.watch
import yaml

# How long to wait before reconnecting the watch stream after an error.
_WATCH_RECONNECT_DELAY = 2

# Path of the heartbeat file checked by the liveness probe.
_HEARTBEAT_PATH = "/tmp/controller-heartbeat"


def _touch_heartbeat() -> None:
    """Touch the heartbeat file to signal the liveness probe that we're alive."""
    try:
        with open(_HEARTBEAT_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _manifest_name(manifest: dict) -> str:
    return manifest["metadata"]["name"]


def _load_manifest(assets_path: str, step_name: str) -> dict:
    path = os.path.join(assets_path, f"step={step_name}", "jobset.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _load_phases(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    dag: list[dict],
) -> dict[str, str]:
    """Load phase state from ConfigMap if it exists; otherwise return all-PENDING.

    Only SUCCEEDED and FAILED states are restored — RUNNING steps are reset to
    PENDING so they will be re-submitted (the 409 Conflict guard in
    _submit_ready_steps handles the case where the JobSet already exists).
    """
    phases: dict[str, str] = {s["name"]: "PENDING" for s in dag}
    cm_name = f"{workflow_id}-phases"
    try:
        cm = k8s_v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
        raw = (cm.data or {}).get("phases")
        if raw:
            saved = json.loads(raw)
            for name, phase in saved.items():
                if name in phases and phase in ("SUCCEEDED", "FAILED"):
                    phases[name] = phase
            print(
                f"[controller] restored phases from ConfigMap: "
                f"{[n for n, p in phases.items() if p != 'PENDING']}",
                flush=True,
            )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            print(f"[controller] warning: could not read phases ConfigMap: {e}", flush=True)
    return phases


def _save_phases(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    phases: dict[str, str],
    owner_ref: list[dict],
) -> None:
    """Persist phase state to a ConfigMap. Best-effort — never raises."""
    cm_name = f"{workflow_id}-phases"
    data = {"phases": json.dumps(phases)}
    try:
        try:
            k8s_v1.patch_namespaced_config_map(
                name=cm_name,
                namespace=namespace,
                body={"data": data},
            )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            # ConfigMap doesn't exist yet — create it.
            cm = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": cm_name,
                    "namespace": namespace,
                    "ownerReferences": owner_ref,
                },
                "data": data,
            }
            k8s_v1.create_namespaced_config_map(namespace=namespace, body=cm)
    except Exception as exc:
        print(f"[controller] warning: could not save phases to ConfigMap: {exc}", flush=True)


def _emit_event(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    job_uid: str,
    reason: str,
    message: str,
    event_type: str = "Normal",
) -> None:
    """Emit a Kubernetes Event on the controller Job. Best-effort — never raises."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        k8s_v1.create_namespaced_event(
            namespace=namespace,
            body={
                "apiVersion": "v1",
                "kind": "Event",
                "metadata": {
                    "name": f"{workflow_id}.{reason.lower()}.{int(time.time())}",
                    "namespace": namespace,
                },
                "involvedObject": {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": workflow_id,
                    "namespace": namespace,
                    "uid": job_uid,
                },
                "reason": reason,
                "message": message,
                "type": event_type,
                "eventTime": now,
                "reportingComponent": "seekr-chain-controller",
                "reportingInstance": workflow_id,
                "action": reason,
            },
        )
    except Exception as exc:
        print(f"[controller] warning: could not emit event {reason!r}: {exc}", flush=True)


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


def _submit_ready_steps(
    dag: list[dict],
    phases: dict[str, str],
    js_names: dict[str, str],
    js_to_step: dict[str, str],
    assets_path: str,
    namespace: str,
    owner_ref: list[dict],
    k8s_custom,
) -> None:
    """Submit any PENDING steps whose dependencies have all SUCCEEDED.

    Updates js_names and js_to_step in place for newly submitted steps.
    Handles 409 Conflict gracefully: if a JobSet already exists (e.g. on
    controller pod retry after a crash), treat it as already submitted.
    """
    for step in dag:
        name = step["name"]
        if phases[name] != "PENDING":
            continue
        deps = step.get("depends_on") or []
        if not all(phases[d] == "SUCCEEDED" for d in deps):
            continue

        manifest = _load_manifest(assets_path, name)
        manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
        js_name = _manifest_name(manifest)

        try:
            k8s_custom.create_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=namespace,
                body=manifest,
            )
            print(f"[controller] submitted step={name!r} jobset={js_name!r}", flush=True)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 409:
                # JobSet already exists — controller was restarted after a crash.
                # Treat as already running; the watch will deliver its terminal
                # state event and we'll advance the DAG normally.
                print(
                    f"[controller] step={name!r} jobset={js_name!r} already exists, resuming",
                    flush=True,
                )
            else:
                raise

        phases[name] = "RUNNING"
        js_names[name] = js_name
        js_to_step[js_name] = name


def main() -> int:
    assets_path = os.environ["SEEKR_CHAIN_JOB_ASSET_PATH"]
    namespace = os.environ["SEEKR_CHAIN_NAMESPACE"]
    job_name = os.environ["SEEKR_CHAIN_CONTROLLER_JOB_NAME"]
    job_uid = os.environ["SEEKR_CHAIN_CONTROLLER_JOB_UID"]
    workflow_id = job_name  # controller Job name == workflow ID

    _touch_heartbeat()

    kubernetes.config.load_incluster_config()
    k8s_custom = kubernetes.client.CustomObjectsApi()
    k8s_v1 = kubernetes.client.CoreV1Api()

    # Load DAG definition from assets
    with open(os.path.join(assets_path, "dag.json")) as f:
        dag = json.load(f)  # [{"name": "a", "depends_on": ["b", ...]}, ...]

    print(f"[controller] loaded DAG with {len(dag)} steps: {[s['name'] for s in dag]}", flush=True)

    # ownerReference so JobSets and the phases ConfigMap are cascade-deleted when
    # this controller Job is deleted.
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

    # Restore persisted phase state so a restarted controller pod resumes correctly.
    phases = _load_phases(k8s_v1, namespace, workflow_id, dag)

    js_names: dict[str, str] = {}
    # reverse map: jobset name -> step name (for event dispatch); updated incrementally
    js_to_step: dict[str, str] = {}

    # Submit all initially-ready steps before opening the watch.
    _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

    if all(p in ("SUCCEEDED", "FAILED") for p in phases.values()):
        # All steps were no-dep and already submitted; nothing to watch.
        # (Can only be terminal here if the DAG has zero steps, which is invalid,
        # but be safe.)
        pass
    else:
        # Watch all JobSets belonging to this workflow. Events arrive immediately
        # when terminalState is set — no polling delay between DAG steps.
        #
        # We reconnect on transient errors, resuming from the last seen
        # resourceVersion so no events are missed. The API server will return a
        # 410 Gone if our resourceVersion is too old (compacted); in that case we
        # fall back to resourceVersion="" which re-lists from the current state.
        resource_version = ""
        label_selector = f"seekr-chain/job-id={workflow_id}"

        while not all(p in ("SUCCEEDED", "FAILED") for p in phases.values()):
            _touch_heartbeat()
            try:
                w = kubernetes.watch.Watch()
                for event in w.stream(
                    k8s_custom.list_namespaced_custom_object,
                    group="jobset.x-k8s.io",
                    version="v1alpha2",
                    plural="jobsets",
                    namespace=namespace,
                    label_selector=label_selector,
                    resource_version=resource_version,
                ):
                    _touch_heartbeat()

                    # Track resourceVersion so a reconnect resumes from here.
                    rv = event.get("object", {}).get("metadata", {}).get("resourceVersion")
                    if rv:
                        resource_version = rv

                    if event["type"] not in ("ADDED", "MODIFIED"):
                        continue

                    obj = event["object"]
                    js_name = obj["metadata"]["name"]
                    terminal = obj.get("status", {}).get("terminalState") or None

                    if not terminal:
                        continue

                    step_name = js_to_step.get(js_name)
                    if step_name is None or phases[step_name] in ("SUCCEEDED", "FAILED"):
                        continue

                    if terminal == "Completed":
                        phases[step_name] = "SUCCEEDED"
                        print(f"[controller] step={step_name!r} SUCCEEDED", flush=True)
                        _emit_event(
                            k8s_v1, namespace, workflow_id, job_uid,
                            "StepSucceeded", f"Step {step_name!r} completed successfully",
                        )
                    elif terminal == "Failed":
                        phases[step_name] = "FAILED"
                        print(f"[controller] step={step_name!r} FAILED", flush=True)
                        _emit_event(
                            k8s_v1, namespace, workflow_id, job_uid,
                            "StepFailed", f"Step {step_name!r} failed",
                            event_type="Warning",
                        )

                    _cascade_fail(dag, phases)
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

                    # Submit any steps now unblocked by this completion.
                    _submit_ready_steps(
                        dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom
                    )
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

                    if all(p in ("SUCCEEDED", "FAILED") for p in phases.values()):
                        w.stop()
                        break

            except kubernetes.client.exceptions.ApiException as e:
                if e.status == 410:
                    # resourceVersion too old — re-list from scratch.
                    print("[controller] watch: resourceVersion expired, re-listing", flush=True)
                    resource_version = ""
                else:
                    print(
                        f"[controller] watch: API error {e.status}, reconnecting in {_WATCH_RECONNECT_DELAY}s",
                        flush=True,
                    )
                    time.sleep(_WATCH_RECONNECT_DELAY)
            except Exception as e:
                print(f"[controller] watch: error ({e}), reconnecting in {_WATCH_RECONNECT_DELAY}s", flush=True)
                time.sleep(_WATCH_RECONNECT_DELAY)

    failed = [n for n, p in phases.items() if p == "FAILED"]
    if failed:
        _emit_event(
            k8s_v1, namespace, workflow_id, job_uid,
            "WorkflowFailed", f"Workflow failed — failed steps: {failed}",
            event_type="Warning",
        )
        print(f"[controller] workflow FAILED — failed steps: {failed}", file=sys.stderr, flush=True)
        return 1

    _emit_event(
        k8s_v1, namespace, workflow_id, job_uid,
        "WorkflowSucceeded", "All steps completed successfully",
    )
    print("[controller] workflow SUCCEEDED — all steps completed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
