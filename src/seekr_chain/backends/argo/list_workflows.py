#!/usr/bin/env python3

import os
from typing import Optional

import kubernetes


def list_argo_workflows(
    namespace: Optional[str] = None, limit: Optional[int] = None, user: Optional[str] = None
) -> list[dict]:
    """List Argo Workflows in the given namespace.

    Returns a list of dicts with keys: name, job_name, user, status, created, duration.
    """
    kubernetes.config.load_kube_config(config_file=os.environ.get("KUBECONFIG"))
    k8s_custom = kubernetes.client.CustomObjectsApi()

    if namespace is None:
        _, active_ctx = kubernetes.config.list_kube_config_contexts()
        namespace = active_ctx["context"].get("namespace", "default")

    label_selector = "seekr-chain/job-id"
    if user is not None:
        label_selector += f",seekr-chain/user={user}"

    kwargs = {
        "group": "argoproj.io",
        "version": "v1alpha1",
        "plural": "workflows",
        "namespace": namespace,
        "label_selector": label_selector,
    }
    if limit is not None:
        kwargs["limit"] = limit

    result = k8s_custom.list_namespaced_custom_object(**kwargs)

    workflows = []
    for wf in result.get("items", []):
        wf_status = wf.get("status", {})
        started_at = wf_status.get("startedAt")
        finished_at = wf_status.get("finishedAt")

        duration = ""
        if started_at:
            from datetime import datetime, timezone

            fmt = "%Y-%m-%dT%H:%M:%SZ"
            dt_start = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
            dt_end = (
                datetime.strptime(finished_at, fmt).replace(tzinfo=timezone.utc)
                if finished_at
                else datetime.now(timezone.utc)
            )
            total_seconds = int((dt_end - dt_start).total_seconds())
            minutes, seconds = divmod(total_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                duration = f"{hours}h{minutes}m{seconds}s"
            elif minutes:
                duration = f"{minutes}m{seconds}s"
            else:
                duration = f"{seconds}s"

        phase = wf_status.get("phase", "Unknown")
        metadata = wf.get("metadata", {})
        labels = metadata.get("labels", {})
        created = metadata.get("creationTimestamp", "")

        workflows.append(
            {
                "name": metadata.get("name", ""),
                "job_name": labels.get("seekr-chain/job-name", ""),
                "user": labels.get("seekr-chain/user", ""),
                "status": phase,
                "created": created,
                "duration": duration,
            }
        )

    return workflows
