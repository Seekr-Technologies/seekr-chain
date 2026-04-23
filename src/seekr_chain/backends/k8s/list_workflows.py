#!/usr/bin/env python3

import os
from datetime import datetime, timezone
from typing import Optional

import kubernetes


def list_k8s_workflows(
    namespace: Optional[str] = None, limit: Optional[int] = None, user: Optional[str] = None
) -> list[dict]:
    """List k8s controller Jobs in the given namespace.

    Returns a list of dicts with keys: name, job_name, user, status, created, duration.
    """
    kubernetes.config.load_kube_config(config_file=os.environ.get("KUBECONFIG"))
    k8s_batch = kubernetes.client.BatchV1Api()

    if namespace is None:
        _, active_ctx = kubernetes.config.list_kube_config_contexts()
        namespace = active_ctx["context"].get("namespace", "default")

    label_selector = "seekr-chain/job-id"
    if user is not None:
        label_selector += f",seekr-chain/user={user}"

    kwargs: dict = {
        "namespace": namespace,
        "label_selector": label_selector,
    }
    if limit is not None:
        kwargs["limit"] = limit

    result = k8s_batch.list_namespaced_job(**kwargs)

    workflows = []
    for job in result.items:
        metadata = job.metadata
        labels = metadata.labels or {}
        status = job.status

        # Determine phase string
        if status.succeeded and status.succeeded > 0:
            phase = "Succeeded"
        elif status.failed and status.failed > 0:
            phase = "Failed"
        elif status.active and status.active > 0:
            phase = "Running"
        else:
            phase = "Pending"

        # Duration calculation
        duration = ""
        start_time = status.start_time
        completion_time = status.completion_time
        if start_time:
            dt_end = completion_time if completion_time else datetime.now(timezone.utc)
            total_seconds = int((dt_end - start_time).total_seconds())
            minutes, seconds = divmod(total_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                duration = f"{hours}h{minutes}m{seconds}s"
            elif minutes:
                duration = f"{minutes}m{seconds}s"
            else:
                duration = f"{seconds}s"

        created = ""
        if metadata.creation_timestamp:
            created = metadata.creation_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

        workflows.append(
            {
                "name": metadata.name or "",
                "job_name": labels.get("seekr-chain/job-name", ""),
                "user": labels.get("seekr-chain/user", ""),
                "status": phase,
                "created": created,
                "duration": duration,
            }
        )

    return workflows
