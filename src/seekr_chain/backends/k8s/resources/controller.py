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
    SEEKR_CHAIN_CONTROLLER_JOB_NAME   Name of this controller JobSet (for ownerReferences; its UID
                                      is self-read at startup via the Kubernetes API, since the
                                      JobSet has no downward-API-exposed pod label for it)

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

# Watch stream timeout: forces the watch to return periodically so the
# heartbeat is touched even when no events arrive (prevents liveness restart
# on a long, event-quiet watch).
_WATCH_TIMEOUT_SECONDS = 30

# Path of the heartbeat file checked by the liveness probe.
_HEARTBEAT_PATH = "/tmp/controller-heartbeat"

# Phases that stop a step from being retried or re-evaluated further. CANCELLED
# covers a JobSet suspended via `chain cancel` (spec.suspend=true) rather than
# one that reached a terminal status — see the watch loop in main().
_TERMINAL_PHASES = ("SUCCEEDED", "FAILED", "CANCELLED")


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

    Only terminal states are restored — RUNNING steps are reset to PENDING so
    they will be re-submitted (the 409 Conflict guard in _submit_ready_steps
    handles the case where the JobSet already exists).
    """
    phases: dict[str, str] = {s["name"]: "PENDING" for s in dag}
    cm_name = f"{workflow_id}-phases"
    try:
        cm = k8s_v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
        raw = (cm.data or {}).get("phases")
        if raw:
            saved = json.loads(raw)
            for name, phase in saved.items():
                if name in phases and phase in _TERMINAL_PHASES:
                    phases[name] = phase
            print(
                f"[controller] restored phases from ConfigMap: {[n for n, p in phases.items() if p != 'PENDING']}",
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
                    "apiVersion": "jobset.x-k8s.io/v1alpha2",
                    "kind": "JobSet",
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


def _jobset_completed_despite_suspend(
    k8s_custom,
    k8s_v1,
    namespace: str,
    workflow_id: str,
    step_name: str,
    js_name: str,
) -> str | None:
    """Disambiguate a bare ``suspend=true`` event from a completion the JobSet
    status hasn't reconciled yet.

    ``chain cancel`` patches ``spec.suspend=true`` on every JobSet. If it fires
    just after the worker pods exit 0 — before the JobSet controller writes
    ``status.terminalState`` — the watch delivers the suspend event first and the
    caller would wrongly record CANCELLED, dropping the real completion. Re-check
    the authoritative signals: a fresh JobSet GET (the watch event may be stale),
    then the worker pods' own phases (more reliable than the JobSet status, which
    is exactly what lags here).

    Returns ``"Completed"`` / ``"Failed"`` if the step actually finished, else
    ``None`` (a genuine mid-flight cancellation).
    """
    try:
        js = k8s_custom.get_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            name=js_name,
        )
        terminal = js.get("status", {}).get("terminalState") or None
        if terminal in ("Completed", "Failed"):
            return terminal
    except Exception as exc:
        print(f"[controller] warning: could not re-read JobSet {js_name!r}: {exc}", flush=True)

    try:
        pods = k8s_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"seekr-chain/job-id={workflow_id},seekr-chain/step={step_name}",
        ).items
    except Exception as exc:
        print(f"[controller] warning: could not list pods for step={step_name!r}: {exc}", flush=True)
        return None

    phases = [p.status.phase for p in pods]
    if phases and all(ph == "Succeeded" for ph in phases):
        return "Completed"
    if "Failed" in phases and not any(ph in ("Pending", "Running", None) for ph in phases):
        return "Failed"
    return None


def _mark_controller_terminal_state(k8s_custom, namespace: str, job_name: str, value: str) -> None:
    """Record the workflow's true terminal state on the controller JobSet as an
    annotation. Best-effort — never raises.

    The controller exits 0 on cancellation (so the JobSet isn't retried), which
    would otherwise be indistinguishable from success. This annotation lets the
    status layer map an exit-0 controller JobSet back to TERMINATED.
    """
    try:
        k8s_custom.patch_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            name=job_name,
            namespace=namespace,
            body={"metadata": {"annotations": {"seekr-chain/terminal-state": value}}},
        )
    except Exception as exc:
        print(f"[controller] warning: could not mark terminal state {value!r}: {exc}", flush=True)


def _cascade_fail(dag: list[dict], phases: dict[str, str]) -> None:
    """Mark PENDING steps whose dependencies (transitively) include a failed or
    cancelled step. A cancelled dependency propagates CANCELLED rather than
    FAILED — the dependent never ran, it was stopped."""
    changed = True
    while changed:
        changed = False
        for step in dag:
            name = step["name"]
            deps = step.get("depends_on") or []
            if phases[name] != "PENDING":
                continue
            if any(phases[d] == "CANCELLED" for d in deps):
                phases[name] = "CANCELLED"
                print(f"[controller] step={name!r} cascade-cancelled", flush=True)
                changed = True
            elif any(phases[d] == "FAILED" for d in deps):
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
            elif e.status == 429 or e.status >= 500:
                # Retriable error (rate limit, server error, gateway timeout) —
                # leave the step PENDING so it is retried on the next watch
                # iteration.  Re-raising would stall the DAG permanently because
                # the watch only re-delivers already-terminal events.
                print(
                    f"[controller] warning: retriable submit error for step={name!r} jobset={js_name!r}: {e}, will retry",
                    flush=True,
                )
                continue
            else:
                # Permanent error (400 malformed manifest, 403 RBAC, 422
                # validation) — fail the step so the DAG doesn't retry forever.
                print(
                    f"[controller] error: permanent submit error for step={name!r} jobset={js_name!r}: {e}, marking FAILED",
                    file=sys.stderr,
                    flush=True,
                )
                phases[name] = "FAILED"
                continue

        phases[name] = "RUNNING"
        js_names[name] = js_name
        js_to_step[js_name] = name


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

    js_names: dict[str, str] = {}
    # reverse map: jobset name -> step name (for event dispatch); updated incrementally
    js_to_step: dict[str, str] = {}

    # Submit all initially-ready steps before opening the watch.
    _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

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
            _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

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
                        # Suspended without a terminalState usually means `chain cancel`
                        # stopped this JobSet. But the suspend event can also race ahead
                        # of a completion the JobSet status hasn't reconciled yet — so
                        # re-check the authoritative signals before committing CANCELLED
                        # (which is terminal and would drop the later completion event).
                        actual = _jobset_completed_despite_suspend(
                            k8s_custom, k8s_v1, namespace, workflow_id, step_name, js_name
                        )
                        if actual == "Completed":
                            phases[step_name] = "SUCCEEDED"
                            print(
                                f"[controller] step={step_name!r} SUCCEEDED (completed before suspend took effect)",
                                flush=True,
                            )
                            _emit_event(
                                k8s_v1,
                                namespace,
                                workflow_id,
                                job_uid,
                                "StepSucceeded",
                                f"Step {step_name!r} completed successfully",
                            )
                        elif actual == "Failed":
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
                        else:
                            # Genuine mid-flight cancellation. Treat it as terminal so the
                            # DAG loop below can exit instead of waiting forever for a
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
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

                    # Submit any steps now unblocked by this completion.
                    _submit_ready_steps(
                        dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom
                    )
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

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
        return 1

    cancelled = [n for n, p in phases.items() if p == "CANCELLED"]
    if cancelled:
        # Cancellation is an intentional terminal state, not an error. Exit 0 so
        # the controller JobSet isn't retried into a restart storm, and record
        # the true state on the JobSet so the status layer can still surface
        # TERMINATED rather than SUCCEEDED.
        _mark_controller_terminal_state(k8s_custom, namespace, workflow_id, "CANCELLED")
        _emit_event(
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "WorkflowCancelled",
            f"Workflow cancelled — cancelled steps: {cancelled}",
        )
        print(f"[controller] workflow CANCELLED — cancelled steps: {cancelled}", flush=True)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
