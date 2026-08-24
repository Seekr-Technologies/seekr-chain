"""Submitting DAG steps whose dependencies are satisfied."""

import sys

import kubernetes

from .manifests import load_manifest, manifest_name, stamp_attempt


def create_jobset(k8s_custom, namespace: str, manifest: dict, js_name: str) -> str:
    """Submit a JobSet manifest to the API server.

    Returns "submitted", "exists" (409 — a JobSet by this name is already
    running, e.g. after a controller restart), "retriable" (429/5xx — caller
    should try again later), or "failed" (permanent error, e.g. 400/403/422).
    Never raises.
    """
    try:
        k8s_custom.create_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            body=manifest,
        )
        return "submitted"
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 409:
            return "exists"
        if e.status == 429 or e.status >= 500:
            # Retriable error (rate limit, server error, gateway timeout) — the
            # caller leaves its step in a non-terminal state so submission is
            # retried on a later watch iteration.
            print(
                f"[controller] warning: retriable submit error for jobset={js_name!r}: {e}, will retry",
                flush=True,
            )
            return "retriable"
        # Permanent error (400 malformed manifest, 403 RBAC, 422 validation).
        print(
            f"[controller] error: permanent submit error for jobset={js_name!r}: {e}",
            file=sys.stderr,
            flush=True,
        )
        return "failed"


def submit_ready_steps(
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

        manifest = load_manifest(assets_path, name)
        manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
        js_name = manifest_name(manifest)

        result = create_jobset(k8s_custom, namespace, manifest, js_name)
        if result == "submitted":
            print(f"[controller] submitted step={name!r} jobset={js_name!r}", flush=True)
        elif result == "exists":
            # Controller was restarted after a crash; the watch will deliver
            # its terminal state event and we'll advance the DAG normally.
            print(f"[controller] step={name!r} jobset={js_name!r} already exists, resuming", flush=True)
        elif result == "retriable":
            # Leave the step PENDING so it is retried on the next watch
            # iteration. Re-raising would stall the DAG permanently because
            # the watch only re-delivers already-terminal events.
            continue
        else:  # "failed"
            phases[name] = "FAILED"
            continue

        phases[name] = "RUNNING"
        js_names[name] = js_name
        js_to_step[js_name] = name


def retry_pending_steps(
    pending_retries: dict[str, int],
    phases: dict[str, str],
    js_names: dict[str, str],
    js_to_step: dict[str, str],
    assets_path: str,
    namespace: str,
    owner_ref: list[dict],
    k8s_custom,
) -> None:
    """Resubmit steps whose previous attempt was judged retriable by
    failure.evaluate_step_failure(), at the next attempt number recorded in
    pending_retries. Steps stay RUNNING throughout — only js_names/js_to_step
    change, to point at the new attempt's JobSet.

    On a retriable API error (429/5xx) the entry is left in pending_retries
    for a later call. On a permanent error, the step is marked FAILED and
    dropped from pending_retries.
    """
    for name, attempt in list(pending_retries.items()):
        # Reload from disk so the manifest is always the pristine attempt-0
        # render — manifest_name() below is therefore always the step's base
        # JobSet name, never a previous attempt's stamped name.
        manifest = load_manifest(assets_path, name)
        manifest.setdefault("metadata", {})["ownerReferences"] = owner_ref
        js_name = f"{manifest_name(manifest)}-a{attempt}"
        stamp_attempt(manifest, js_name, attempt)

        result = create_jobset(k8s_custom, namespace, manifest, js_name)
        if result == "retriable":
            continue
        if result == "failed":
            phases[name] = "FAILED"
            del pending_retries[name]
            continue

        verb = "submitted" if result == "submitted" else "already exists, resuming"
        print(f"[controller] step={name!r} retry {verb} as jobset={js_name!r} (attempt {attempt})", flush=True)
        js_names[name] = js_name
        js_to_step[js_name] = name
        del pending_retries[name]
