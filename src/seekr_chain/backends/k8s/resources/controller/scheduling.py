"""Submitting DAG steps whose dependencies are satisfied."""

import sys

import kubernetes

from .manifests import load_manifest, manifest_name
from .phases import dep_satisfied, normalize_dep
from .status_model import Status


def submit_ready_steps(
    dag: list[dict],
    phases: dict[str, str],
    js_names: dict[str, str],
    js_to_step: dict[str, str],
    assets_path: str,
    namespace: str,
    owner_ref: list[dict],
    k8s_custom,
    exit_codes: dict[str, list[int]] | None = None,
) -> None:
    """Submit any PENDING steps whose `depends_on` conditions are all satisfied.

    Updates js_names and js_to_step in place for newly submitted steps.
    Handles 409 Conflict gracefully: if a JobSet already exists (e.g. on
    controller pod retry after a crash), treat it as already submitted.
    """
    exit_codes = exit_codes or {}
    for step in dag:
        name = step["name"]
        if phases[name] != Status.PENDING.value:
            continue
        deps = [normalize_dep(d) for d in (step.get("depends_on") or [])]
        if not all(dep_satisfied(phases[d["step"]], d, exit_codes) for d in deps):
            continue

        manifest = load_manifest(assets_path, name)
        manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
        js_name = manifest_name(manifest)

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
                phases[name] = Status.FAILED.value
                continue

        phases[name] = Status.RUNNING.value
        js_names[name] = js_name
        js_to_step[js_name] = name


def cancel_step_jobsets(step_names: list[str], js_names: dict[str, str], namespace: str, k8s_custom) -> None:
    """Suspend (spec.suspend=true) the JobSets for `step_names` without
    deleting them — the same patch `chain cancel` uses
    (K8sWorkflow.cancel()). A future watch event observes the resulting
    suspend and marks the step CANCELED; this call only requests it.

    Best-effort: a step not yet in `js_names` (never actually submitted, or
    already gone) is skipped, and a patch failure for one step doesn't stop
    the others from being cancelled.
    """
    for name in step_names:
        js_name = js_names.get(name)
        if js_name is None:
            continue
        try:
            k8s_custom.patch_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=namespace,
                name=js_name,
                body={"spec": {"suspend": True}},
            )
            print(f"[controller] cancelling step={name!r} jobset={js_name!r} (workflow failed)", flush=True)
        except kubernetes.client.exceptions.ApiException as e:
            print(
                f"[controller] warning: failed to cancel jobset={js_name!r} for step={name!r}: {e}",
                flush=True,
            )
