"""Phase state: persistence, restore, and cascade pre-emption."""

import json

import kubernetes

from .status_model import Status

# Phases that stop a step from being retried or re-evaluated further. CANCELED
# covers a JobSet suspended via `chain cancel` (spec.suspend=true) rather than
# one that reached a terminal status — see the watch loop in main(). SKIPPED
# covers a step pre-empted by a non-succeeding dependency: it never ran, so it
# is distinct from FAILED (ran and failed) and CANCELED (user cancelled it
# directly) — see cascade_fail().
TERMINAL_PHASES = tuple(s.value for s in Status if s.is_terminal())


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
) -> None:
    """Persist phase and timing state to a ConfigMap. Best-effort — never raises."""
    cm_name = f"{workflow_id}-phases"
    data = {"phases": json.dumps(phases), "timings": json.dumps(timings)}
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


def cascade_fail(dag: list[dict], phases: dict[str, str]) -> None:
    """Mark PENDING steps whose dependencies (transitively) include a step that
    did not succeed as SKIPPED — the dependent never ran, it was pre-empted.
    This is distinct from FAILED (the step itself ran and failed) and CANCELED
    (the user cancelled that step directly). SKIPPED is itself a cascade
    trigger, so a chain of pre-empted steps fully propagates within the
    fixpoint loop below."""
    changed = True
    while changed:
        changed = False
        for step in dag:
            name = step["name"]
            deps = step.get("depends_on") or []
            if phases[name] != Status.PENDING.value:
                continue
            if any(Status(phases[d]).is_failed() or phases[d] == Status.SKIPPED.value for d in deps):
                phases[name] = Status.SKIPPED.value
                print(f"[controller] step={name!r} SKIPPED (upstream did not succeed)", flush=True)
                changed = True
