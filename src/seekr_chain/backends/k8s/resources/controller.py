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


def _load_handlers(assets_path: str) -> dict[str, list[dict]]:
    """Load ``handlers.json`` grouped by parent step name.

    Missing file (assets packaged before exit handlers existed) -> {}.
    """
    path = os.path.join(assets_path, "handlers.json")
    try:
        with open(path) as f:
            entries = json.load(f)
    except FileNotFoundError:
        return {}
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["parent"], []).append(entry)
    return grouped


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


def _default_exit_info() -> dict:
    return {
        "exit_code": None,
        "reason": "",
        "message": "",
        "oom_killed": False,
        "pod": "",
        "role": "",
        "pod_exits": [],
    }


def _read_step_exit_info(k8s_v1, namespace: str, workflow_id: str, step_name: str) -> dict:
    """Read the ``main`` container's terminated state for every pod of a step.

    Picks a representative pod (prefers a nonzero exit code, then OOMKilled,
    then the latest finish time) so a handler has a single exit
    code/reason/message to react to, while ``pod_exits`` still carries the
    per-pod detail for multi-pod steps. Best-effort: pods may already be
    GC'd or RBAC may briefly fail during a controller restart, so any error
    yields the all-empty default rather than propagating into the watch loop.
    """
    try:
        resp = k8s_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"seekr-chain/job-id={workflow_id},seekr-chain/step={step_name}",
        )
        pod_exits = []
        for pod in resp.items:
            role = (pod.metadata.labels or {}).get("seekr-chain/role", "")
            terminated = None
            for cs in pod.status.container_statuses or []:
                if cs.name == "main":
                    terminated = cs.state.terminated
                    break
            reason = (terminated.reason or "") if terminated else ""
            pod_exits.append(
                {
                    "pod": pod.metadata.name,
                    "role": role,
                    "exit_code": terminated.exit_code if terminated else None,
                    "reason": reason,
                    "message": (terminated.message or "") if terminated else "",
                    "oom_killed": reason == "OOMKilled",
                    "finished_at": terminated.finished_at if terminated else None,
                }
            )

        if not pod_exits:
            return _default_exit_info()

        _epoch = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

        def _sort_key(p: dict) -> tuple:
            return (
                1 if p["exit_code"] not in (None, 0) else 0,
                1 if p["oom_killed"] else 0,
                p["finished_at"] or _epoch,
            )

        representative = max(pod_exits, key=_sort_key)

        return {
            "exit_code": representative["exit_code"],
            "reason": representative["reason"],
            "message": representative["message"],
            "oom_killed": representative["oom_killed"],
            "pod": representative["pod"],
            "role": representative["role"],
            "pod_exits": [{k: v for k, v in p.items() if k != "finished_at"} for p in pod_exits],
        }
    except Exception:
        return _default_exit_info()


def _handler_env(
    handler_entry: dict,
    parent_step: str,
    parent_jobset: str,
    parent_status: str,
    exit_info: dict,
) -> list[dict]:
    """Build the env entries a handler pod sees describing why its parent step ended."""
    exit_code = exit_info.get("exit_code")
    return [
        {"name": "SEEKR_CHAIN_HANDLER_NAME", "value": handler_entry["name"]},
        {"name": "SEEKR_CHAIN_HANDLER_WHEN", "value": handler_entry["when"]},
        {"name": "SEEKR_CHAIN_PARENT_STEP", "value": parent_step},
        {"name": "SEEKR_CHAIN_PARENT_JOBSET", "value": parent_jobset},
        {"name": "SEEKR_CHAIN_PARENT_STATUS", "value": parent_status},
        {"name": "SEEKR_CHAIN_PARENT_EXIT_CODE", "value": "" if exit_code is None else str(exit_code)},
        {"name": "SEEKR_CHAIN_PARENT_FAILURE_REASON", "value": exit_info.get("reason") or ""},
        {"name": "SEEKR_CHAIN_PARENT_FAILURE_MESSAGE", "value": exit_info.get("message") or ""},
        {"name": "SEEKR_CHAIN_PARENT_OOM_KILLED", "value": "true" if exit_info.get("oom_killed") else "false"},
        {"name": "SEEKR_CHAIN_PARENT_POD", "value": exit_info.get("pod") or ""},
        {"name": "SEEKR_CHAIN_PARENT_ROLE", "value": exit_info.get("role") or ""},
        {"name": "SEEKR_CHAIN_PARENT_POD_EXITS", "value": json.dumps(exit_info.get("pod_exits") or [])},
    ]


def _inject_handler_env(manifest: dict, env_entries: list[dict]) -> None:
    """Append env entries to the ``main`` container of a handler JobSet manifest.

    Other containers (``chain-init``, ``log-sidecar``) are left untouched.
    """
    for replicated_job in manifest.get("spec", {}).get("replicatedJobs", []):
        containers = (
            replicated_job.get("template", {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        )
        for container in containers:
            if container.get("name") == "main":
                container.setdefault("env", []).extend(env_entries)


def _load_handler_states(k8s_v1, namespace: str, workflow_id: str) -> dict[str, str]:
    """Load handler state from the same ConfigMap as phases, under the
    ``"handlers"`` data key (kept separate from ``"phases"``).

    Unlike ``_load_phases``, SUBMITTED is restored as-is (not reset to
    PENDING) to avoid double env-injection/submission on controller
    restart — the 409 Conflict guard on submit is the backstop.
    """
    cm_name = f"{workflow_id}-phases"
    try:
        cm = k8s_v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
        raw = (cm.data or {}).get("handlers")
        if raw:
            return json.loads(raw)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            print(f"[controller] warning: could not read handler states ConfigMap: {e}", flush=True)
    return {}


def _save_handler_states(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    states: dict[str, str],
) -> None:
    """Persist handler state to the phases ConfigMap's ``"handlers"`` key. Best-effort."""
    cm_name = f"{workflow_id}-phases"
    data = {"handlers": json.dumps(states)}
    try:
        k8s_v1.patch_namespaced_config_map(
            name=cm_name,
            namespace=namespace,
            body={"data": data},
        )
    except Exception as exc:
        print(f"[controller] warning: could not save handler states to ConfigMap: {exc}", flush=True)


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


def _workflow_settled(
    dag: list[dict],
    phases: dict[str, str],
    handlers: dict[str, list[dict]],
    handler_states: dict[str, str],
) -> bool:
    """True once every step phase is terminal and no handler is still outstanding.

    "Outstanding" means SUBMITTED (running), or PENDING with a parent step
    that has already gone terminal (it is about to be dispatched or gated
    out). A step with no declared handlers settles exactly as today.
    """
    if not all(phases[s["name"]] in _TERMINAL_PHASES for s in dag):
        return False
    for parent_step, entries in handlers.items():
        parent_phase = phases.get(parent_step)
        for entry in entries:
            state = handler_states.get(entry["step"], "PENDING")
            if state == "SUBMITTED":
                return False
            if state == "PENDING" and parent_phase in _TERMINAL_PHASES:
                return False
    return True


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


def _submit_handlers_for_step(
    step_name: str,
    phases: dict[str, str],
    handlers: dict[str, list[dict]],
    handler_states: dict[str, str],
    js_names: dict[str, str],
    js_to_handler: dict[str, str],
    assets_path: str,
    namespace: str,
    workflow_id: str,
    owner_ref: list[dict],
    k8s_custom,
    k8s_v1,
) -> None:
    """Dispatch any still-PENDING exit handlers of a step that has gone terminal.

    Gates on ``when`` vs the step's terminal phase, then on ``on_exit_codes`` if
    the handler declares one (reading the parent's exit info at most once per
    call). Updates handler_states and js_to_handler in place. Never touches
    ``phases`` or calls ``_cascade_fail`` — a handler's outcome is invisible to
    the DAG by construction.
    """
    entries = handlers.get(step_name)
    if not entries:
        return

    parent_phase = phases[step_name]
    pending = [e for e in entries if handler_states.get(e["step"], "PENDING") == "PENDING"]
    if not pending:
        return

    if parent_phase == "CANCELLED":
        for entry in pending:
            handler_states[entry["step"]] = "SKIPPED"
            print(
                f"[controller] handler={entry['name']!r} step={entry['step']!r} SKIPPED (parent cancelled)",
                flush=True,
            )
        return

    if parent_phase == "SUCCEEDED":
        allowed_when = ("on_success", "always")
    elif parent_phase == "FAILED":
        allowed_when = ("on_failure", "always")
    else:
        # Parent not yet terminal (shouldn't normally be reached — callers only
        # invoke this for terminal steps) — nothing to dispatch yet.
        return

    parent_jobset = js_names.get(step_name, "")
    exit_info = None

    for entry in pending:
        if entry["when"] not in allowed_when:
            handler_states[entry["step"]] = "SKIPPED"
            print(
                f"[controller] handler={entry['name']!r} step={entry['step']!r} SKIPPED (when={entry['when']!r} vs {parent_phase!r})",
                flush=True,
            )
            continue

        on_exit_codes = entry.get("on_exit_codes")
        if exit_info is None and (on_exit_codes is not None):
            exit_info = _read_step_exit_info(k8s_v1, namespace, workflow_id, step_name)
        if on_exit_codes is not None and exit_info.get("exit_code") not in on_exit_codes:
            handler_states[entry["step"]] = "SKIPPED"
            print(
                f"[controller] handler={entry['name']!r} step={entry['step']!r} SKIPPED (exit_code={exit_info.get('exit_code')!r} not in {on_exit_codes})",
                flush=True,
            )
            continue

        if exit_info is None:
            exit_info = _read_step_exit_info(k8s_v1, namespace, workflow_id, step_name)

        pseudo = entry["step"]
        manifest = _load_manifest(assets_path, pseudo)
        manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
        env_entries = _handler_env(entry, step_name, parent_jobset, parent_phase, exit_info)
        _inject_handler_env(manifest, env_entries)
        js_name = _manifest_name(manifest)

        try:
            k8s_custom.create_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=namespace,
                body=manifest,
            )
            print(
                f"[controller] submitted handler={entry['name']!r} step={entry['step']!r} jobset={js_name!r}",
                flush=True,
            )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 409:
                # JobSet already exists — controller restarted after a crash,
                # or a prior iteration submitted it. Treat as already running.
                print(
                    f"[controller] handler={entry['name']!r} jobset={js_name!r} already exists, resuming",
                    flush=True,
                )
            elif e.status == 429 or e.status >= 500:
                print(
                    f"[controller] warning: retriable submit error for handler={entry['name']!r} jobset={js_name!r}: {e}, will retry",
                    flush=True,
                )
                continue  # leave PENDING for the next dispatch attempt
            else:
                print(
                    f"[controller] error: permanent submit error for handler={entry['name']!r} jobset={js_name!r}: {e}, marking FAILED",
                    file=sys.stderr,
                    flush=True,
                )
                handler_states[entry["step"]] = "FAILED"
                continue

        handler_states[entry["step"]] = "SUBMITTED"
        js_to_handler[js_name] = pseudo


def _dispatch_handlers_for_terminal_steps(
    dag: list[dict],
    phases: dict[str, str],
    handlers: dict[str, list[dict]],
    handler_states: dict[str, str],
    js_names: dict[str, str],
    js_to_handler: dict[str, str],
    assets_path: str,
    namespace: str,
    workflow_id: str,
    owner_ref: list[dict],
    k8s_custom,
    k8s_v1,
) -> None:
    """Run _submit_handlers_for_step over every step currently in a terminal phase.

    Used both for restart-safety (before opening the watch) and as a retry
    pass each watch iteration, mirroring how _submit_ready_steps is retried.
    """
    for step in dag:
        name = step["name"]
        if phases[name] in _TERMINAL_PHASES:
            _submit_handlers_for_step(
                name,
                phases,
                handlers,
                handler_states,
                js_names,
                js_to_handler,
                assets_path,
                namespace,
                workflow_id,
                owner_ref,
                k8s_custom,
                k8s_v1,
            )


def _restore_submitted_handler_jobsets(
    handlers: dict[str, list[dict]],
    handler_states: dict[str, str],
    assets_path: str,
    js_to_handler: dict[str, str],
) -> None:
    """Repopulate js_to_handler for handlers already SUBMITTED by a prior
    controller incarnation, without re-submitting them.

    _submit_handlers_for_step only builds js_to_handler at the moment it
    submits a handler; a restored SUBMITTED state is intentionally never
    re-dispatched (that's what avoids double env-injection), so without this
    the watch loop would have no way to attribute that handler's terminal
    JobSet event back to it and the controller would wait for it forever.
    """
    for entries in handlers.values():
        for entry in entries:
            if handler_states.get(entry["step"]) == "SUBMITTED":
                manifest = _load_manifest(assets_path, entry["step"])
                js_to_handler[_manifest_name(manifest)] = entry["step"]


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

    # Exit handlers: grouped by parent step (empty for assets packaged before
    # exit handlers existed) and their dispatch state, restored the same way
    # phases are (SUBMITTED kept as-is — the 409 guard is the backstop).
    handlers = _load_handlers(assets_path)
    handler_states = _load_handler_states(k8s_v1, namespace, workflow_id)
    drain_timeout = float(os.environ.get("SEEKR_CHAIN_HANDLER_DRAIN_TIMEOUT", "3600"))

    js_names: dict[str, str] = {}
    # reverse map: jobset name -> step name (for event dispatch); updated incrementally
    js_to_step: dict[str, str] = {}
    # reverse map: jobset name -> handler pseudo-step name; checked before js_to_step
    # in the watch loop so handler outcomes never reach _cascade_fail/phases.
    js_to_handler: dict[str, str] = {}
    _restore_submitted_handler_jobsets(handlers, handler_states, assets_path, js_to_handler)

    # Submit all initially-ready steps before opening the watch.
    _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

    # Dispatch handlers for any steps restored as already-terminal (restart
    # safety: a step may have gone terminal in a prior controller incarnation).
    _dispatch_handlers_for_terminal_steps(
        dag,
        phases,
        handlers,
        handler_states,
        js_names,
        js_to_handler,
        assets_path,
        namespace,
        workflow_id,
        owner_ref,
        k8s_custom,
        k8s_v1,
    )
    _save_handler_states(k8s_v1, namespace, workflow_id, handler_states)

    # Time at which every step (not necessarily every handler) first went
    # terminal — used to bound how long we wait for handlers to drain below.
    steps_terminal_since: float | None = None

    if _workflow_settled(dag, phases, handlers, handler_states):
        # All steps were no-dep and already submitted, and no handler is
        # outstanding; nothing to watch.
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

        while not _workflow_settled(dag, phases, handlers, handler_states):
            _touch_heartbeat()

            # Retry any steps that failed to submit on a previous iteration
            # (retriable API errors leave them PENDING).  Also cascade-fail
            # dependents of any step marked FAILED by a permanent submit error.
            _submit_ready_steps(dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom)
            _cascade_fail(dag, phases)
            _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

            # Retry any handlers still PENDING (e.g. a previous dispatch hit a
            # retriable submit error) for steps that are already terminal.
            _dispatch_handlers_for_terminal_steps(
                dag,
                phases,
                handlers,
                handler_states,
                js_names,
                js_to_handler,
                assets_path,
                namespace,
                workflow_id,
                owner_ref,
                k8s_custom,
                k8s_v1,
            )
            _save_handler_states(k8s_v1, namespace, workflow_id, handler_states)

            if all(p in _TERMINAL_PHASES for p in phases.values()):
                if steps_terminal_since is None:
                    steps_terminal_since = time.time()
                elif time.time() - steps_terminal_since > drain_timeout:
                    print(
                        f"[controller] warning: handler drain timeout ({drain_timeout}s) exceeded, giving up on outstanding handlers",
                        flush=True,
                    )
                    _emit_event(
                        k8s_v1,
                        namespace,
                        workflow_id,
                        job_uid,
                        "HandlerDrainTimeout",
                        f"Handlers still outstanding {drain_timeout}s after all steps went terminal, giving up",
                        event_type="Warning",
                    )
                    break
            else:
                steps_terminal_since = None

            if _workflow_settled(dag, phases, handlers, handler_states):
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

                    terminal = obj.get("status", {}).get("terminalState") or None
                    suspended = obj.get("spec", {}).get("suspend", False)

                    # Handler JobSets are checked first so their outcome never
                    # reaches js_to_step / _cascade_fail / phases below.
                    handler_step = js_to_handler.get(js_name)
                    if handler_step is not None:
                        if handler_states.get(handler_step) in _TERMINAL_PHASES:
                            continue
                        if terminal == "Completed":
                            handler_states[handler_step] = "SUCCEEDED"
                            print(f"[controller] handler={handler_step!r} SUCCEEDED", flush=True)
                            _emit_event(
                                k8s_v1,
                                namespace,
                                workflow_id,
                                job_uid,
                                "HandlerSucceeded",
                                f"Handler {handler_step!r} completed successfully",
                            )
                        elif terminal == "Failed":
                            handler_states[handler_step] = "FAILED"
                            print(f"[controller] handler={handler_step!r} FAILED", flush=True)
                            _emit_event(
                                k8s_v1,
                                namespace,
                                workflow_id,
                                job_uid,
                                "HandlerFailed",
                                f"Handler {handler_step!r} failed",
                                event_type="Warning",
                            )
                        elif suspended:
                            handler_states[handler_step] = "CANCELLED"
                            print(f"[controller] handler={handler_step!r} CANCELLED", flush=True)
                            _emit_event(
                                k8s_v1,
                                namespace,
                                workflow_id,
                                job_uid,
                                "HandlerCancelled",
                                f"Handler {handler_step!r} was cancelled",
                            )
                        else:
                            continue
                        _save_handler_states(k8s_v1, namespace, workflow_id, handler_states)
                        continue

                    step_name = js_to_step.get(js_name)
                    if step_name is None or phases[step_name] in _TERMINAL_PHASES:
                        continue

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
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

                    # Dispatch any handlers gated on this step's outcome.
                    _submit_handlers_for_step(
                        step_name,
                        phases,
                        handlers,
                        handler_states,
                        js_names,
                        js_to_handler,
                        assets_path,
                        namespace,
                        workflow_id,
                        owner_ref,
                        k8s_custom,
                        k8s_v1,
                    )
                    _save_handler_states(k8s_v1, namespace, workflow_id, handler_states)

                    # Submit any steps now unblocked by this completion.
                    _submit_ready_steps(
                        dag, phases, js_names, js_to_step, assets_path, namespace, owner_ref, k8s_custom
                    )
                    _save_phases(k8s_v1, namespace, workflow_id, phases, owner_ref)

                    if _workflow_settled(dag, phases, handlers, handler_states):
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

    failed_handlers = [n for n, s in handler_states.items() if s == "FAILED"]
    if failed_handlers:
        print(f"[controller] handlers failed (does not affect workflow status): {failed_handlers}", flush=True)
        _emit_event(
            k8s_v1,
            namespace,
            workflow_id,
            job_uid,
            "HandlersFailed",
            f"Handlers failed (does not affect workflow status): {failed_handlers}",
        )

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
