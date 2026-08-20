"""main() — the controller pod entrypoint: DAG submission and watch loop."""

import datetime
import json
import os
import sys
import time

import kubernetes
import kubernetes.watch

from .events import _emit_event, _touch_heartbeat
from .phases import _TERMINAL_PHASES, _cascade_fail, _load_phases, _save_phases
from .scheduling import _submit_ready_steps
from .status import _write_status


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stamp_starts(dag: list[dict], phases: dict[str, str], timings: dict[str, dict]) -> None:
    """Record dt_start for any step that just left PENDING (i.e. was submitted)."""
    for step in dag:
        name = step["name"]
        if phases[name] != "PENDING" and "dt_start" not in timings.setdefault(name, {}):
            timings[name]["dt_start"] = _now_iso()


def _stamp_ends(phases: dict[str, str], timings: dict[str, dict]) -> None:
    """Record dt_end for any step that has reached a terminal phase."""
    for name, phase in phases.items():
        if phase in _TERMINAL_PHASES and "dt_end" not in timings.setdefault(name, {}):
            timings[name]["dt_end"] = _now_iso()


# How long to wait before reconnecting the watch stream after an error.
_WATCH_RECONNECT_DELAY = 2

# Watch stream timeout: forces the watch to return periodically so the
# heartbeat is touched even when no events arrive (prevents liveness restart
# on a long, event-quiet watch).
_WATCH_TIMEOUT_SECONDS = 30


def main() -> int:
    assets_path = os.environ["SEEKR_CHAIN_JOB_ASSET_PATH"]
    namespace = os.environ["SEEKR_CHAIN_NAMESPACE"]
    job_name = os.environ["SEEKR_CHAIN_CONTROLLER_JOB_NAME"]
    workflow_id = job_name  # controller JobSet name == workflow ID

    _touch_heartbeat()

    kubernetes.config.load_incluster_config()
    k8s_custom = kubernetes.client.CustomObjectsApi()
    k8s_v1 = kubernetes.client.CoreV1Api()

    # Self-read our own JobSet's UID. There's no downward-API field for a JobSet's
    # UID (unlike a Job's controller-uid pod label), but jobset.x-k8s.io/jobsets:get
    # is RBAC every ServiceAccount that can run this controller already has.
    controller_jobset = k8s_custom.get_namespaced_custom_object(
        group="jobset.x-k8s.io",
        version="v1alpha2",
        plural="jobsets",
        namespace=namespace,
        name=job_name,
    )
    job_uid = controller_jobset["metadata"]["uid"]

    # Load DAG definition from assets
    with open(os.path.join(assets_path, "dag.json")) as f:
        dag = json.load(f)  # [{"name": "a", "depends_on": ["b", ...]}, ...]

    print(f"[controller] loaded DAG with {len(dag)} steps: {[s['name'] for s in dag]}", flush=True)

    # ownerReference so JobSets and the phases ConfigMap are cascade-deleted when
    # this controller JobSet is deleted.
    owner_ref = [
        {
            "apiVersion": "jobset.x-k8s.io/v1alpha2",
            "kind": "JobSet",
            "name": job_name,
            "uid": job_uid,
            "blockOwnerDeletion": True,
            "controller": True,
        }
    ]

    # Restore persisted phase state so a restarted controller pod resumes correctly.
    phases = _load_phases(k8s_v1, namespace, workflow_id, dag)

    # In-memory only (dark launch, v1) — lost on a controller restart, which is
    # acceptable: status.json is an outcome doc, not the source of truth (the
    # phases ConfigMap is). Not persisted alongside phases to keep the
    # ConfigMap small and avoid a schema migration later.
    timings: dict[str, dict] = {}
    _write_status(workflow_id, dag, phases, timings)

    js_names: dict[str, str] = {}
    # reverse map: jobset name -> step name (for event dispatch); updated incrementally
    js_to_step: dict[str, str] = {}

    # Submit all initially-ready steps before opening the watch.
    _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
    _stamp_starts(dag, phases, timings)
    _stamp_ends(phases, timings)
    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)
    _write_status(workflow_id, dag, phases, timings)

    if all(p in _TERMINAL_PHASES for p in phases.values()):
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

        while not all(p in _TERMINAL_PHASES for p in phases.values()):
            _touch_heartbeat()

            # Retry any steps that failed to submit on a previous iteration
            # (retriable API errors leave them PENDING).  Also cascade-fail
            # dependents of any step marked FAILED by a permanent submit error.
            _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
            _cascade_fail(dag, phases)
            _stamp_starts(dag, phases, timings)
            _stamp_ends(phases, timings)
            _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)
            _write_status(workflow_id, dag, phases, timings)

            if all(p in _TERMINAL_PHASES for p in phases.values()):
                break

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
                    timeout_seconds=_WATCH_TIMEOUT_SECONDS,
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

                    step_name = js_to_step.get(js_name)
                    if step_name is None or phases[step_name] in _TERMINAL_PHASES:
                        continue

                    terminal = obj.get("status", {}).get("terminalState") or None
                    suspended = obj.get("spec", {}).get("suspend", False)

                    if terminal == "Completed":
                        phases[step_name] = "SUCCEEDED"
                        print(f"[controller] step={step_name!r} SUCCEEDED", flush=True)
                        _emit_event(
                            k8s_v1,
                            namespace,
                            workflow_id,
                            job_uid,
                            "StepSucceeded",
                            f"Step {step_name!r} completed successfully",
                        )
                    elif terminal == "Failed":
                        phases[step_name] = "FAILED"
                        print(f"[controller] step={step_name!r} FAILED", flush=True)
                        _emit_event(
                            k8s_v1,
                            namespace,
                            workflow_id,
                            job_uid,
                            "StepFailed",
                            f"Step {step_name!r} failed",
                            event_type="Warning",
                        )
                    elif suspended:
                        # Suspended without a terminalState means `chain cancel` (or
                        # any other spec.suspend=true patch) stopped this JobSet — not
                        # a normal completion. Treat it as terminal so the DAG loop
                        # below can exit instead of waiting forever for a
                        # terminalState that will never arrive.
                        phases[step_name] = "CANCELLED"
                        print(f"[controller] step={step_name!r} CANCELLED", flush=True)
                        _emit_event(
                            k8s_v1,
                            namespace,
                            workflow_id,
                            job_uid,
                            "StepCancelled",
                            f"Step {step_name!r} was cancelled",
                        )
                    else:
                        continue

                    _cascade_fail(dag, phases)
                    _stamp_ends(phases, timings)
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)
                    _write_status(workflow_id, dag, phases, timings)

                    # Submit any steps now unblocked by this completion.
                    _submit_ready_steps(
                        dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom
                    )
                    _stamp_starts(dag, phases, timings)
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)
                    _write_status(workflow_id, dag, phases, timings)

                    if all(p in _TERMINAL_PHASES for p in phases.values()):
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
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "WorkflowFailed",
            f"Workflow failed — failed steps: {failed}",
            event_type="Warning",
        )
        print(f"[controller] workflow FAILED — failed steps: {failed}", file=sys.stderr, flush=True)
        _write_status(workflow_id, dag, phases, timings)
        return 0

    cancelled = [n for n, p in phases.items() if p == "CANCELLED"]
    if cancelled:
        _emit_event(
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "WorkflowCancelled",
            f"Workflow cancelled — cancelled steps: {cancelled}",
        )
        print(f"[controller] workflow CANCELLED — cancelled steps: {cancelled}", flush=True)
        _write_status(workflow_id, dag, phases, timings)
        return 0

    _emit_event(
        k8s_v1,
        namespace,
        workflow_id,
        job_uid,
        "WorkflowSucceeded",
        "All steps completed successfully",
    )
    print("[controller] workflow SUCCEEDED — all steps completed", flush=True)
    _write_status(workflow_id, dag, phases, timings)
    return 0
