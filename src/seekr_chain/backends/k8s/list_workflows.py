#!/usr/bin/env python3

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from seekr_chain.backends.k8s.workflow_state import (
    _parse_timestamp,
    controller_jobset_status_and_completion,
    read_phases_configmap,
)
from seekr_chain.k8s_api import kube

_CONFIGMAP_READ_WORKERS = 32

_PHASE_BY_STATUS = {
    "SUCCEEDED": "Succeeded",
    "FAILED": "Failed",
    "RUNNING": "Running",
    "CANCELED": "Canceled",
    "ERROR": "Error",
}


def _profile(message: str) -> None:
    print(f"[chain list profile] {message}", file=sys.stderr, flush=True)


def list_k8s_workflows(
    namespace: Optional[str] = None, limit: Optional[int] = None, user: Optional[str] = None
) -> list[dict]:
    """List controller JobSets in the given namespace.

    When ``limit`` is non-zero, phase state is fetched only for the most recent
    finished workflows. Active workflows are always retained.

    Returns a list of dicts with keys: name, job_name, user, status, created, duration.
    """
    total_started = time.perf_counter()

    started = time.perf_counter()
    k8s_custom = kube.custom_objects
    _profile(f"initialize CustomObjects API: {time.perf_counter() - started:.3f}s")

    started = time.perf_counter()
    k8s_v1 = kube.core_v1
    _profile(f"initialize CoreV1 API: {time.perf_counter() - started:.3f}s")

    if namespace is None:
        started = time.perf_counter()
        namespace = kube.namespace
        _profile(f"resolve namespace: {time.perf_counter() - started:.3f}s")

    label_selector = "seekr-chain/job-id,seekr-chain/is-controller=true"
    if user is not None:
        label_selector += f",seekr-chain/user={user}"

    kwargs: dict = {
        "group": "jobset.x-k8s.io",
        "version": "v1alpha2",
        "plural": "jobsets",
        "namespace": namespace,
        "label_selector": label_selector,
    }
    started = time.perf_counter()
    result = k8s_custom.list_namespaced_custom_object(**kwargs)
    items = result.get("items", [])
    _profile(f"list {len(items)} JobSets: {time.perf_counter() - started:.3f}s")

    if limit:
        finished = [
            jobset for jobset in items if jobset.get("status", {}).get("terminalState") in ("Completed", "Failed")
        ]
        active = [
            jobset for jobset in items if jobset.get("status", {}).get("terminalState") not in ("Completed", "Failed")
        ]
        finished.sort(key=lambda jobset: jobset.get("metadata", {}).get("creationTimestamp") or "")
        items = finished[-limit:] + active
        _profile(f"apply early limit: {len(items)} JobSets retained ({len(active)} active)")

    completed_indices = [
        index for index, jobset in enumerate(items) if jobset.get("status", {}).get("terminalState") == "Completed"
    ]

    def read_completed_phases(index: int):
        metadata = items[index].get("metadata", {})
        workflow_id = metadata.get("name") or "<unknown>"
        started = time.perf_counter()
        phases_configmap = read_phases_configmap(k8s_v1, namespace, metadata.get("name"))
        elapsed = time.perf_counter() - started
        _profile(f"{workflow_id}: read phases ConfigMap: {elapsed:.3f}s")
        return index, phases_configmap, elapsed

    phases_configmaps = {}
    configmap_seconds = 0.0
    started = time.perf_counter()
    if completed_indices:
        with ThreadPoolExecutor(max_workers=_CONFIGMAP_READ_WORKERS) as executor:
            for index, phases_configmap, elapsed in executor.map(read_completed_phases, completed_indices):
                phases_configmaps[index] = phases_configmap
                configmap_seconds += elapsed
    configmap_wall_seconds = time.perf_counter() - started
    _profile(
        f"phases ConfigMap reads ({len(completed_indices)} requests, {_CONFIGMAP_READ_WORKERS} workers): "
        f"{configmap_wall_seconds:.3f}s wall / {configmap_seconds:.3f}s cumulative"
    )

    workflows = []
    for index, jobset in enumerate(items):
        item_started = time.perf_counter()
        metadata = jobset.get("metadata", {})
        labels = metadata.get("labels", {}) or {}
        workflow_id = metadata.get("name") or "<unknown>"

        status, completion_time = controller_jobset_status_and_completion(jobset, phases_configmaps.get(index))
        phase = _PHASE_BY_STATUS.get(status.value, "Pending")

        # Duration calculation
        duration = ""
        conditions = jobset.get("status", {}).get("conditions", []) or []
        all_times = [c.get("lastTransitionTime") for c in conditions if c.get("lastTransitionTime")]
        start_time = (
            _parse_timestamp(min(all_times)) if all_times else _parse_timestamp(metadata.get("creationTimestamp"))
        )
        if start_time:
            dt_end = completion_time if completion_time else datetime.now(timezone.utc)
            total_seconds = int((dt_end - start_time).total_seconds())
            minutes, seconds = divmod(total_seconds, 60)
            hours, minutes = divmod(minutes, 60)
            if hours:
                duration = f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                duration = f"{minutes}:{seconds:02d}"

        created = ""
        if metadata.get("creationTimestamp"):
            created = metadata["creationTimestamp"]

        workflows.append(
            {
                "name": metadata.get("name") or "",
                "job_name": labels.get("seekr-chain/job-name", ""),
                "user": labels.get("seekr-chain/user", ""),
                "status": phase,
                "created": created,
                "duration": duration,
            }
        )
        _profile(f"{workflow_id}: process JobSet: {time.perf_counter() - item_started:.3f}s")

    _profile(f"backend total ({len(workflows)} workflows): {time.perf_counter() - total_started:.3f}s")
    return workflows
