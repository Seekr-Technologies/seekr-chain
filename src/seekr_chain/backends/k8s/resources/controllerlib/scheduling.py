"""Submitting DAG steps whose dependencies are satisfied."""

import sys

import kubernetes

from .manifests import _load_manifest, _manifest_name


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
