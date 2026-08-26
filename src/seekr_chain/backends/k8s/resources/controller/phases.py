"""Phase state: persistence, restore, dead-end detection, and failure teardown."""

import json

import kubernetes

from .status_model import Status

# Phases that stop a step from being retried or re-evaluated further. CANCELED
# covers a JobSet suspended via `chain cancel` (spec.suspend=true) rather than
# one that reached a terminal status — see the watch loop in main(). SKIPPED
# covers a step pre-empted by a non-succeeding dependency or by a failure
# teardown: it never ran, so it is distinct from FAILED (ran and failed) and
# CANCELED (user, or a failure teardown, cancelled it directly) — see
# skip_dead_ends() and apply_failure_teardown().
TERMINAL_PHASES = tuple(s.value for s in Status if s.is_terminal())

# Phases meaning a dependency did not succeed — a valid ON_FAILURE/ALWAYS
# trigger regardless of *why* it didn't succeed (it ran and failed, was
# cancelled directly, or was itself pre-empted upstream).
_NOT_SUCCEEDED_PHASES = ("FAILED", "CANCELED", "SKIPPED")


def normalize_dep(entry) -> dict:
    """Coerce a dag.json depends_on entry (bare string or dict) into full dict shape.

    dag.json is plain JSON (see launch_k8s_workflow.py's dag_entries builder),
    so entries are either a bare step name string (today's ON_SUCCESS-required
    semantics) or an already-structured dict.
    """
    if isinstance(entry, str):
        return {"step": entry, "when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN"}
    return {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", **entry}


def dep_satisfied(phase: str, cond: dict, exit_codes: dict[str, list[int]]) -> bool:
    """True if a `depends_on` condition is satisfied given the dependency's phase."""
    if phase not in TERMINAL_PHASES:
        return False
    if cond["when"] == "ALWAYS":
        return True
    if cond["when"] == "ON_SUCCESS":
        return phase == "SUCCEEDED"
    # ON_FAILURE
    if phase not in _NOT_SUCCEEDED_PHASES:
        return False
    if cond["on_exit_codes"] is None:
        return True
    matched = any(code in cond["on_exit_codes"] for code in exit_codes.get(cond["step"], []))
    return matched if cond["operator"] == "IN" else not matched


def load_phases(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    dag: list[dict],
) -> tuple[dict[str, str], dict[str, dict]]:
    """Load phase and timing state from ConfigMap if it exists; otherwise
    return all-PENDING phases and no timings.

    Only terminal states are restored — RUNNING steps are reset to PENDING so
    they will be re-submitted (the 409 Conflict guard in submit_ready_steps
    handles the case where the JobSet already exists). Timings follow the
    same rule: a step's timings are restored only if its phase is restored
    (i.e. terminal), so a RUNNING step reset to PENDING also loses its
    timings and gets re-stamped on re-run.
    """
    phases: dict[str, str] = {s["name"]: Status.PENDING.value for s in dag}
    cm_name = f"{workflow_id}-phases"
    timings: dict[str, dict] = {}
    try:
        cm = k8s_v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
        raw = (cm.data or {}).get("phases")
        if raw:
            saved = json.loads(raw)
            for name, phase in saved.items():
                if name in phases and phase in TERMINAL_PHASES:
                    phases[name] = phase
            print(
                f"[controller] restored phases from ConfigMap: "
                f"{[n for n, p in phases.items() if p != Status.PENDING.value]}",
                flush=True,
            )
        raw_timings = (cm.data or {}).get("timings")
        if raw_timings:
            for name, t in json.loads(raw_timings).items():
                if name in phases and phases[name] in TERMINAL_PHASES:
                    timings[name] = t
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            print(f"[controller] warning: could not read phases ConfigMap: {e}", flush=True)
    return phases, timings


def save_phases(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    phases: dict[str, str],
    timings: dict[str, dict],
    owner_ref: list[dict],
    exit_codes: dict[str, list[int]] | None = None,
) -> None:
    """Persist phase and timing state to a ConfigMap. Best-effort — never raises.

    `exit_codes` is an additional key in the same ConfigMap, so a restarted
    controller doesn't need to re-list pods for steps it already resolved.
    """
    cm_name = f"{workflow_id}-phases"
    data = {"phases": json.dumps(phases), "timings": json.dumps(timings)}
    if exit_codes is not None:
        data["exit_codes"] = json.dumps(exit_codes)
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


def skip_dead_ends(dag: list[dict], phases: dict[str, str], exit_codes: dict[str, list[int]] | None = None) -> None:
    """Mark PENDING steps SKIPPED once their `depends_on` can never be
    satisfied (e.g. an ON_FAILURE edge whose target SUCCEEDED) — a dead end.
    Such a step would never be submitted, so without this it would stay
    PENDING forever and the workflow would never reach a terminal state.

    An ON_SUCCESS dep that already didn't succeed is a dead end immediately,
    without waiting for sibling deps to resolve too — an AND-required edge
    that already failed can never be satisfied regardless of what else is
    still pending.

    Independent of whether any step has FAILED elsewhere — this only concerns
    a step whose own trigger condition can never fire. Runs to a fixpoint
    since a chain of dead ends fully propagates within one call.
    """
    exit_codes = exit_codes or {}
    changed = True
    while changed:
        changed = False
        for step in dag:
            name = step["name"]
            if phases[name] != Status.PENDING.value:
                continue
            deps = [normalize_dep(d) for d in (step.get("depends_on") or [])]
            success_deps = [d["step"] for d in deps if d["when"] == "ON_SUCCESS"]
            if any(phases[d] in _NOT_SUCCEEDED_PHASES for d in success_deps):
                phases[name] = Status.SKIPPED.value
                print(f"[controller] step={name!r} SKIPPED (upstream did not succeed)", flush=True)
                changed = True
            elif (
                deps
                and all(phases[d["step"]] in TERMINAL_PHASES for d in deps)
                and not all(dep_satisfied(phases[d["step"]], d, exit_codes) for d in deps)
            ):
                phases[name] = Status.SKIPPED.value
                print(f"[controller] step={name!r} SKIPPED (unsatisfiable depends_on)", flush=True)
                changed = True


def reactive_preserve_set(
    dag: list[dict], phases: dict[str, str], exit_codes: dict[str, list[int]] | None = None
) -> set[str]:
    """Steps with a satisfied ON_FAILURE/ALWAYS `depends_on` edge onto a
    FAILED step — the direct reactive dependents a failure teardown
    (`apply_failure_teardown`) must let run to completion. The reactive-only
    dead-end validator (config.py's `check_depends_on`) guarantees such a step
    has no dependents of its own, so this is always exactly one hop.
    """
    exit_codes = exit_codes or {}
    preserve: set[str] = set()
    for step in dag:
        for entry in step.get("depends_on") or []:
            cond = normalize_dep(entry)
            if (
                cond["when"] in ("ON_FAILURE", "ALWAYS")
                and phases.get(cond["step"]) == "FAILED"
                and dep_satisfied(phases[cond["step"]], cond, exit_codes)
            ):
                preserve.add(step["name"])
    return preserve


def apply_failure_teardown(
    dag: list[dict], phases: dict[str, str], exit_codes: dict[str, list[int]] | None = None
) -> list[str]:
    """A failed step always fails the workflow, no exceptions. Once any step
    is FAILED: mark every other PENDING step SKIPPED (it will never run) and
    return the names of every other RUNNING step, for the caller to cancel —
    this module has no k8s client, so the actual JobSet suspend patch is the
    caller's job (see scheduling.cancel_step_jobsets).

    Direct reactive (ON_FAILURE/ALWAYS) dependents of the failed step(s) —
    see `reactive_preserve_set` — are exempt from both: they're left PENDING
    (to be picked up by `submit_ready_steps`) or RUNNING (left alone) so the
    workflow's cleanup/notification steps actually get to run.
    """
    if not any(phase == "FAILED" for phase in phases.values()):
        return []
    preserve = reactive_preserve_set(dag, phases, exit_codes)
    to_cancel = []
    for step in dag:
        name = step["name"]
        if name in preserve:
            continue
        if phases[name] == "PENDING":
            phases[name] = "SKIPPED"
            print(f"[controller] step={name!r} SKIPPED (workflow failed)", flush=True)
        elif phases[name] == "RUNNING":
            to_cancel.append(name)
    return to_cancel


def load_exit_codes(k8s_v1, namespace: str, workflow_id: str) -> dict[str, list[int]]:
    """Load persisted exit codes from the phases ConfigMap; empty dict if absent."""
    cm_name = f"{workflow_id}-phases"
    try:
        cm = k8s_v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
        raw = (cm.data or {}).get("exit_codes")
        return json.loads(raw) if raw else {}
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            print(f"[controller] warning: could not read exit_codes from ConfigMap: {e}", flush=True)
        return {}


def steps_needing_exit_codes(dag: list[dict]) -> set[str]:
    """Step names that at least one `depends_on.on_exit_codes` condition gates on."""
    needed = set()
    for step in dag:
        for entry in step.get("depends_on") or []:
            cond = normalize_dep(entry)
            if cond["on_exit_codes"] is not None:
                needed.add(cond["step"])
    return needed


def capture_exit_codes(k8s_v1, namespace: str, workflow_id: str, step_name: str) -> list[int]:
    """List a step's worker pods and read each container's terminated exit code.

    Only called for steps in `steps_needing_exit_codes` — a narrower, on-demand
    version of listing pods, paid only for steps something actually gates on.
    Best-effort — returns [] on error rather than stalling the DAG.
    """
    try:
        pods = k8s_v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"seekr-chain/job-id={workflow_id},seekr-chain/step={step_name}",
        )
    except Exception as exc:
        print(f"[controller] warning: could not list pods for step={step_name!r} exit codes: {exc}", flush=True)
        return []
    codes = set()
    for pod in pods.items:
        for cs in pod.status.container_statuses or []:
            terminated = cs.state.terminated
            if terminated is not None:
                codes.add(terminated.exit_code)
    return sorted(codes)
