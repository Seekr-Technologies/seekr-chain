"""main() — the controller pod entrypoint: DAG submission and watch loop."""

import json
import os
import sys
import time

import kubernetes
import kubernetes.watch

from .events import emit_event, touch_heartbeat
from .phases import (
    TERMINAL_PHASES,
    apply_failure_teardown,
    capture_exit_codes,
    load_exit_codes,
    load_phases,
    save_phases,
    skip_dead_ends,
    steps_needing_exit_codes,
)
from .scheduling import cancel_step_jobsets, submit_ready_steps
from .status import flush_status, write_status
from .status_model import Status
from .timeutil import now_iso


def _stamp_starts(dag: list[dict], phases: dict[str, str], timings: dict[str, dict]) -> None:
    """Record dt_start for any step that actually started running. SKIPPED (and
    cancelled-from-pending) steps never ran, so they must not get a run
    timestamp — checking phase != PENDING isn't enough, since those phases are
    also non-PENDING once the workflow finishes.

    now_iso() here is the controller's *observation* time, not the pod's
    actual start time — approximate, and can lag across a watch reconnect.
    A future improvement is sourcing this from the pod itself."""
    for step in dag:
        name = step["name"]
        if phases[name] in (
            Status.RUNNING.value,
            Status.SUCCEEDED.value,
            Status.FAILED.value,
        ) and "dt_start" not in timings.setdefault(name, {}):
            timings[name]["dt_start"] = now_iso()


def _stamp_ends(phases: dict[str, str], timings: dict[str, dict]) -> None:
    """Record dt_end for any step that reached a terminal phase after having
    actually started (has a dt_start). This both keeps SKIPPED steps free of
    run timestamps and prevents dt_end from ever being stamped before
    dt_start in the cascade-fail branch.

    now_iso() here is the controller's *observation* time, not the pod's
    actual finish time — approximate, and can lag across a watch reconnect.
    A future improvement is sourcing this from the pod itself."""
    for name, phase in phases.items():
        if (
            phase in TERMINAL_PHASES
            and "dt_start" in timings.get(name, {})
            and "dt_end" not in timings.setdefault(name, {})
        ):
            timings[name]["dt_end"] = now_iso()


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

    touch_heartbeat()

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

    # Precompute once: steps whose exit code some depends_on.on_exit_codes gates on.
    needs_exit_codes = steps_needing_exit_codes(dag)

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

    # Restore persisted phase and timing state so a restarted controller pod
    # resumes correctly.
    phases, timings = load_phases(k8s_v1, namespace, workflow_id, dag)
    exit_codes = load_exit_codes(k8s_v1, namespace, workflow_id)

    # Timings are persisted alongside phases in the ConfigMap and restored
    # (terminal-only) on restart. They remain a best-effort outcome detail,
    # not control-flow state.
    write_status(workflow_id, dag, phases, timings)

    js_names: dict[str, str] = {}
    # reverse map: jobset name -> step name (for event dispatch); updated incrementally
    js_to_step: dict[str, str] = {}

    # Submit all initially-ready steps before opening the watch.
    submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom, exit_codes)
    _stamp_starts(dag, phases, timings)
    _stamp_ends(phases, timings)
    save_phases(k8s_v1, namespace, workflow_id, phases, timings, owner_ref, exit_codes)
    write_status(workflow_id, dag, phases, timings)

    if all(p in TERMINAL_PHASES for p in phases.values()):
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

        while not all(p in TERMINAL_PHASES for p in phases.values()):
            touch_heartbeat()

            # Retry any steps that failed to submit on a previous iteration
            # (retriable API errors leave them PENDING). Also apply failure
            # teardown for any step marked FAILED by a permanent submit error.
            submit_ready_steps(
                dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom, exit_codes
            )
            skip_dead_ends(dag, phases, exit_codes)
            to_cancel = apply_failure_teardown(dag, phases, exit_codes)
            if to_cancel:
                cancel_step_jobsets(to_cancel, js_names, namespace, k8s_custom)
            _stamp_starts(dag, phases, timings)
            _stamp_ends(phases, timings)
            save_phases(k8s_v1, namespace, workflow_id, phases, timings, owner_ref, exit_codes)
            write_status(workflow_id, dag, phases, timings)

            if all(p in TERMINAL_PHASES for p in phases.values()):
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
                    touch_heartbeat()

                    # Track resourceVersion so a reconnect resumes from here.
                    rv = event.get("object", {}).get("metadata", {}).get("resourceVersion")
                    if rv:
                        resource_version = rv

                    if event["type"] not in ("ADDED", "MODIFIED"):
                        continue

                    obj = event["object"]
                    js_name = obj["metadata"]["name"]

                    step_name = js_to_step.get(js_name)
                    if step_name is None or phases[step_name] in TERMINAL_PHASES:
                        continue

                    terminal = obj.get("status", {}).get("terminalState") or None
                    suspended = obj.get("spec", {}).get("suspend", False)

                    if terminal == "Completed":
                        phases[step_name] = Status.SUCCEEDED.value
                        print(f"[controller] step={step_name!r} SUCCEEDED", flush=True)
                        emit_event(
                            k8s_v1,
                            namespace,
                            workflow_id,
                            job_uid,
                            "StepSucceeded",
                            f"Step {step_name!r} completed successfully",
                        )
                    elif terminal == "Failed":
                        phases[step_name] = Status.FAILED.value
                        print(f"[controller] step={step_name!r} FAILED", flush=True)
                        emit_event(
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
                        phases[step_name] = Status.CANCELED.value
                        print(f"[controller] step={step_name!r} CANCELED", flush=True)
                        emit_event(
                            k8s_v1,
                            namespace,
                            workflow_id,
                            job_uid,
                            "StepCancelled",
                            f"Step {step_name!r} was cancelled",
                        )
                    else:
                        continue

                    if terminal in ("Completed", "Failed") and step_name in needs_exit_codes:
                        exit_codes[step_name] = capture_exit_codes(k8s_v1, namespace, workflow_id, step_name)
                        print(
                            f"[controller] step={step_name!r} exit_codes={exit_codes[step_name]}",
                            flush=True,
                        )

                    skip_dead_ends(dag, phases, exit_codes)
                    to_cancel = apply_failure_teardown(dag, phases, exit_codes)
                    if to_cancel:
                        cancel_step_jobsets(to_cancel, js_names, namespace, k8s_custom)
                    _stamp_ends(phases, timings)
                    save_phases(k8s_v1, namespace, workflow_id, phases, timings, owner_ref, exit_codes)
                    write_status(workflow_id, dag, phases, timings)

                    # Submit any steps now unblocked by this completion.
                    submit_ready_steps(
                        dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom, exit_codes
                    )
                    _stamp_starts(dag, phases, timings)
                    save_phases(k8s_v1, namespace, workflow_id, phases, timings, owner_ref, exit_codes)
                    write_status(workflow_id, dag, phases, timings)

                    if all(p in TERMINAL_PHASES for p in phases.values()):
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

    failed = [n for n, p in phases.items() if p == Status.FAILED.value]
    if failed:
        emit_event(
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "WorkflowFailed",
            f"Workflow failed — failed steps: {failed}",
            event_type="Warning",
        )
        print(f"[controller] workflow FAILED — failed steps: {failed}", file=sys.stderr, flush=True)
        flush_status(workflow_id, dag, phases, timings)
        return 0

    cancelled = [n for n, p in phases.items() if p == Status.CANCELED.value]
    if cancelled:
        emit_event(
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "WorkflowCancelled",
            f"Workflow cancelled — cancelled steps: {cancelled}",
        )
        print(f"[controller] workflow CANCELLED — cancelled steps: {cancelled}", flush=True)
        flush_status(workflow_id, dag, phases, timings)
        return 0

    emit_event(
        k8s_v1,
        namespace,
        workflow_id,
        job_uid,
        "WorkflowSucceeded",
        "All steps completed successfully",
    )
    print("[controller] workflow SUCCEEDED — all steps completed", flush=True)
    flush_status(workflow_id, dag, phases, timings)
    return 0
