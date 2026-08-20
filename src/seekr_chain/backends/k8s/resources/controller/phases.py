"""Phase state: persistence, restore, and cascade pre-emption."""

import json

import kubernetes

# Phases that stop a step from being retried or re-evaluated further. CANCELLED
# covers a JobSet suspended via `chain cancel` (spec.suspend=true) rather than
# one that reached a terminal status — see the watch loop in main(). SKIPPED
# covers a step pre-empted by a non-succeeding dependency: it never ran, so it
# is distinct from FAILED (ran and failed) and CANCELLED (user cancelled it
# directly) — see cascade_fail().
TERMINAL_PHASES = ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED")


def load_phases(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    dag: list[dict],
) -> dict[str, str]:
    """Load phase state from ConfigMap if it exists; otherwise return all-PENDING.

    Only terminal states are restored — RUNNING steps are reset to PENDING so
    they will be re-submitted (the 409 Conflict guard in submit_ready_steps
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
                if name in phases and phase in TERMINAL_PHASES:
                    phases[name] = phase
            print(
                f"[controller] restored phases from ConfigMap: {[n for n, p in phases.items() if p != 'PENDING']}",
                flush=True,
            )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != 404:
            print(f"[controller] warning: could not read phases ConfigMap: {e}", flush=True)
    return phases


def save_phases(
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


def cascade_fail(dag: list[dict], phases: dict[str, str]) -> None:
    """Mark PENDING steps whose dependencies (transitively) include a step that
    did not succeed as SKIPPED — the dependent never ran, it was pre-empted.
    This is distinct from FAILED (the step itself ran and failed) and CANCELLED
    (the user cancelled that step directly). SKIPPED is itself a cascade
    trigger, so a chain of pre-empted steps fully propagates within the
    fixpoint loop below."""
    changed = True
    while changed:
        changed = False
        for step in dag:
            name = step["name"]
            deps = step.get("depends_on") or []
            if phases[name] != "PENDING":
                continue
            if any(phases[d] in ("FAILED", "CANCELLED", "SKIPPED") for d in deps):
                phases[name] = "SKIPPED"
                print(f"[controller] step={name!r} SKIPPED (upstream did not succeed)", flush=True)
                changed = True
