#!/usr/bin/env python3
"""
Workflow status retrieval: reads JobSets and pods from the Kubernetes API
and builds the ``WorkflowState`` data structure consumed by the rendering
layer (``render_status``) and by the ``K8sWorkflow`` class.

Public API:

  * Dataclasses: ``ContainerState``, ``PodState``, ``RoleState``,
    ``StepState``, ``WorkflowState`` — the data contract between this
    module and consumers.
  * ``get_workflow_state(...)`` — the main entry point. Returns a
    fully-populated ``WorkflowState``.
  * ``get_workflow_job_status(...)`` — the controller Job's status,
    used for the workflow-level header line.
  * ``first_running_or_finished_pod(...)`` — used by ``attach()`` to
    pick a pod to ``kubectl exec`` into.
  * ``is_jobset_suspended(...)`` — used by ``cancel()``.
"""

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

import kubernetes as k8s

from seekr_chain.status import ContainerStatus, PodStatus, WorkflowStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures — the contract between retrieval and rendering
# ---------------------------------------------------------------------------


@dataclass
class ContainerState:
    name: str
    status: ContainerStatus
    dt_start: Optional[datetime.datetime]
    dt_end: Optional[datetime.datetime]
    message: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class PodState:
    dt_start: Optional[datetime.datetime]
    dt_end: Optional[datetime.datetime]
    status: PodStatus
    init_containers: list[ContainerState]
    containers: list[ContainerState]
    name: str
    job_index: int
    job_global_index: int
    restart_attempt: int


@dataclass
class RoleState:
    dt_start: Optional[datetime.datetime]
    dt_end: Optional[datetime.datetime]
    name: Optional[str]
    pods: list[PodState]
    status: PodStatus


@dataclass
class StepState:
    dt_start: Optional[datetime.datetime]
    dt_end: Optional[datetime.datetime]
    name: Optional[str]
    roles: list[RoleState]
    pod: PodState


@dataclass
class WorkflowState:
    """A point-in-time snapshot of a workflow's full state.

    All workflow-level metadata (id, name, status, timing, total step count)
    lives here alongside the per-step/role/pod tree, so the rendering layer
    can consume a single object without needing extra context from the
    caller. ``captured_at`` records when the snapshot was taken — the
    rendering layer uses it as the header timestamp.
    """

    id: str
    name: Optional[str]  # ``seekr-chain/job-name`` label, or None if not set
    status: WorkflowStatus
    dt_start: Optional[datetime.datetime]  # workflow start (controller Job start_time)
    dt_end: Optional[datetime.datetime]  # workflow completion (terminal only)
    total_steps: Optional[int]  # ``seekr-chain/step-count`` annotation
    captured_at: datetime.datetime  # wall-clock time at snapshot
    steps: list[StepState]


# ---------------------------------------------------------------------------
# Internal: timestamp parsing & message helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(ts):
    if isinstance(ts, datetime.datetime):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
    return None


_PULL_ERROR_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
_SKIP_WAITING_MESSAGE = {"CrashLoopBackOff", "ContainerCreating", "PodInitializing"}


def _trim_pull_message(message: str) -> str:
    if message.startswith("Back-off "):
        marker = "ErrImagePull: "
        idx = message.find(marker)
        if idx != -1:
            message = message[idx + len(marker) :]
    return message


# ---------------------------------------------------------------------------
# Internal: per-container / per-pod / per-role / per-step collectors
# ---------------------------------------------------------------------------


def _populate_from_waiting(state: ContainerState, waiting, is_init: bool) -> None:
    """Set status and message from a V1ContainerStateWaiting."""
    reason = waiting.reason or ""
    if reason in _PULL_ERROR_REASONS:
        state.status = ContainerStatus.PULL_ERROR
    elif is_init:
        state.status = ContainerStatus.INIT_WAITING
    else:
        state.status = ContainerStatus.WAITING
    if reason not in _SKIP_WAITING_MESSAGE:
        raw = waiting.message or None
        if raw and reason in _PULL_ERROR_REASONS:
            raw = _trim_pull_message(raw)
        state.message = raw or None


def _populate_from_terminated(state: ContainerState, terminated, is_init: bool) -> None:
    """Set status, reason, and timestamps from a V1ContainerStateTerminated."""
    if terminated.exit_code == 0:
        state.status = ContainerStatus.SUCCEEDED
    else:
        state.status = ContainerStatus.INIT_ERROR if is_init else ContainerStatus.FAILED
        if (terminated.reason or "") == "OOMKilled":
            state.reason = "OOMKilled"
    state.dt_start = _parse_timestamp(terminated.started_at)
    state.dt_end = _parse_timestamp(terminated.finished_at)


def _populate_from_running(state: ContainerState, running, is_init: bool) -> None:
    """Set status and start time from a V1ContainerStateRunning."""
    state.status = ContainerStatus.INIT_RUNNING if is_init else ContainerStatus.RUNNING
    state.dt_start = _parse_timestamp(running.started_at)


def _container_state_from(container_status, is_init: bool) -> ContainerState:
    """Build a ``ContainerState`` from one ``V1ContainerStatus``."""
    state = ContainerState(
        name=container_status.name,
        dt_start=None,
        dt_end=None,
        status=ContainerStatus.UNKNOWN,
    )
    cs = container_status.state
    if cs.waiting:
        _populate_from_waiting(state, cs.waiting, is_init)
    elif cs.terminated:
        _populate_from_terminated(state, cs.terminated, is_init)
    elif cs.running:
        _populate_from_running(state, cs.running, is_init)
    else:
        # The Kubernetes API guarantees V1ContainerState has exactly one of
        # waiting/running/terminated set (protobuf oneof). This branch should
        # never be reached, but we degrade gracefully rather than crashing
        # `chain status` if a future client version drifts.
        logger.warning(
            "Unexpected container state for %r: not waiting, running, or terminated — treating as UNKNOWN",
            container_status.name,
        )
    return state


def _collect_container_states(
    container_statuses: list[k8s.client.models.v1_container_status.V1ContainerStatus],
    is_init: bool = False,
) -> list[ContainerState]:
    if not container_statuses:
        return []
    return [_container_state_from(cs, is_init) for cs in container_statuses]


def _resolve_status(statuses: list[ContainerStatus], rules: list[tuple]) -> Optional[PodStatus]:
    """Apply container-status → pod-status translation rules in order.

    Each rule is a tuple:
      ``(container_status, pod_status)``         — match if ANY container has the status
      ``(container_status, pod_status, "all")``  — match if ALL containers have the status

    Returns the first matching pod_status, or ``None`` if no rule matches.
    """
    for rule in rules:
        src, dst = rule[0], rule[1]
        check = all if len(rule) == 3 and rule[2] == "all" else any
        if check(s == src for s in statuses):
            return dst
    return None


def _derive_pod_status(
    pod_phase: str,
    init_statuses: list[ContainerStatus],
    main_statuses: list[ContainerStatus],
) -> PodStatus:
    """Derive overall pod status from container statuses + the pod phase.

    Precedence:
      1. Pod phase already terminal (SUCCEEDED/FAILED) — use it directly.
      2. PULL_ERROR on any container — short-circuit.
      3. Main containers progressing — RUNNING, or transient FAILED/RUNNING when
         main has terminated but the pod phase hasn't flipped yet.
      4. Init containers — INIT_RUNNING / INIT_ERROR / PULLING / INIT_WAITING.
      5. Main containers exist but haven't started (no init container) — PULLING.
      6. Nothing yet — PENDING.
    """
    if pod_phase in ("SUCCEEDED", "FAILED"):
        return PodStatus(pod_phase)

    pull_error = _resolve_status(
        init_statuses + main_statuses,
        [
            (ContainerStatus.PULL_ERROR, PodStatus.PULL_ERROR),
        ],
    )
    if pull_error:
        return pull_error

    # Main containers have terminated but pod phase hasn't updated yet (transient):
    # show FAILED eagerly; otherwise keep RUNNING until the phase flips.
    main_result = _resolve_status(
        main_statuses,
        [
            (ContainerStatus.RUNNING, PodStatus.RUNNING),
            (ContainerStatus.FAILED, PodStatus.FAILED),
            (ContainerStatus.SUCCEEDED, PodStatus.RUNNING),
        ],
    )
    if main_result:
        return main_result

    if init_statuses:
        return (
            _resolve_status(
                init_statuses,
                [
                    (ContainerStatus.INIT_RUNNING, PodStatus.INIT_RUNNING),
                    (ContainerStatus.INIT_ERROR, PodStatus.INIT_ERROR),
                    # All init containers done → main image is being pulled.
                    (ContainerStatus.SUCCEEDED, PodStatus.PULLING, "all"),
                ],
            )
            or PodStatus.INIT_WAITING
        )

    if main_statuses:
        return PodStatus.PULLING

    return PodStatus.PENDING


def _finalize_pod_times(pod_state: PodState) -> None:
    """Apply pod time semantics to ``pod_state`` in place.

    - Before main containers start (PENDING / INIT:* / PULLING), count from
      pod.start_time so the user sees how long setup has been taking.
    - Once any main container has started, reset ``dt_start`` to the earliest
      main container start so the displayed duration is "how long has the
      actual work been running".
    - ``dt_end`` finalizes only when the pod itself is terminal — otherwise
      an active pod whose init containers have already completed would
      freeze its dt_end at the init-completion timestamp and stop advancing
      (e.g. PULLING pods would show a static "0:01").
    """
    main_dt_starts = [c.dt_start for c in pod_state.containers if c.dt_start]
    if main_dt_starts:
        pod_state.dt_start = min(main_dt_starts)
    if not pod_state.status.is_finished():
        return
    if main_dt_starts:
        main_dt_ends = [c.dt_end for c in pod_state.containers if c.dt_end]
        if main_dt_ends:
            pod_state.dt_end = max(main_dt_ends)
    else:
        dt_ends = [c.dt_end for c in pod_state.init_containers + pod_state.containers if c.dt_end]
        if dt_ends:
            pod_state.dt_end = max(dt_ends)


def _collect_pod_state(pod) -> PodState:
    pod_state = PodState(
        dt_start=_parse_timestamp(pod.status.start_time),
        dt_end=None,
        name=pod.metadata.name,
        status=PodStatus.UNKNOWN,
        init_containers=_collect_container_states(pod.status.init_container_statuses, is_init=True),
        containers=_collect_container_states(pod.status.container_statuses, is_init=False),
        job_index=int(pod.metadata.labels.get("jobset.sigs.k8s.io/job-index", 0)),
        job_global_index=int(pod.metadata.labels.get("jobset.sigs.k8s.io/job-global-index", 0)),
        restart_attempt=int(pod.metadata.labels.get("jobset.sigs.k8s.io/restart-attempt", 0)),
    )
    pod_state.status = _derive_pod_status(
        pod_phase=(pod.status.phase or "UNKNOWN").upper(),
        init_statuses=[c.status for c in pod_state.init_containers],
        main_statuses=[c.status for c in pod_state.containers],
    )
    _finalize_pod_times(pod_state)
    return pod_state


def _collect_role_state(role_name, role_pods) -> RoleState:
    out = RoleState(
        dt_start=None,
        dt_end=None,
        name=role_name,
        pods=[_collect_pod_state(role_pod) for role_pod in role_pods],
        status=PodStatus("UNKNOWN"),
    )

    dt_starts = [pod.dt_start for pod in out.pods if pod.dt_start]
    if dt_starts:
        out.dt_start = min(dt_starts)
    dt_ends = [pod.dt_end for pod in out.pods if pod.dt_end]
    if dt_ends:
        out.dt_end = max(dt_ends)

    out.status = min([pod.status for pod in out.pods])
    return out


def _jobset_step_pod(step_name: str, jobset: dict, role_states: list[RoleState]) -> PodState:
    """Derive a virtual PodState for a step from the JobSet and worker pod states.

    - Kueue suspension and terminal states come from the JobSet resource so they
      appear immediately (before any pods exist).
    - The RUNNING check uses actual worker pod statuses, since
      replicatedJobsStatus.active can lag or be zero while pods are live.
    """
    spec = jobset.get("spec", {})
    status = jobset.get("status", {})

    if spec.get("suspend", False):
        pod_status = PodStatus.PENDING
    elif status.get("terminalState") == "Completed":
        pod_status = PodStatus.SUCCEEDED
    elif status.get("terminalState") == "Failed":
        pod_status = PodStatus.FAILED
    else:
        # Derive from worker pod statuses — more reliable than replicatedJobsStatus.active.
        # Any status beyond PENDING/UNKNOWN means the pod has been scheduled and is active.
        all_pod_statuses = [pod.status for role in role_states for pod in role.pods]
        if any(s not in (PodStatus.PENDING, PodStatus.UNKNOWN) for s in all_pod_statuses):
            pod_status = min(all_pod_statuses)
        else:
            pod_status = PodStatus.PENDING

    conditions = status.get("conditions", []) or []
    all_times = [c.get("lastTransitionTime") for c in conditions if c.get("lastTransitionTime")]
    dt_start = _parse_timestamp(min(all_times)) if all_times else None

    dt_end = None
    terminal_state = status.get("terminalState")
    if terminal_state in ("Completed", "Failed"):
        terminal_times = [
            c.get("lastTransitionTime")
            for c in conditions
            if c.get("type") == terminal_state and c.get("lastTransitionTime")
        ]
        if terminal_times:
            dt_end = _parse_timestamp(max(terminal_times))
        elif all_times:
            dt_end = _parse_timestamp(max(all_times))

    return PodState(
        name=step_name,
        status=pod_status,
        dt_start=dt_start,
        dt_end=dt_end,
        init_containers=[],
        containers=[],
        job_index=0,
        job_global_index=0,
        restart_attempt=0,
    )


def _collect_step_state(name, roles, jobset: dict) -> StepState:
    role_states = [_collect_role_state(role_name, role_pods) for role_name, role_pods in roles.items()]
    step_pod = _jobset_step_pod(name, jobset, role_states)
    return StepState(
        dt_start=step_pod.dt_start,
        dt_end=step_pod.dt_end,
        name=name,
        roles=role_states,
        pod=step_pod,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _job_status_and_completion(job) -> tuple[WorkflowStatus, Optional[datetime.datetime]]:
    """Map a Kubernetes Job's ``.status`` into ``(WorkflowStatus, completion_time)``.

    ``completion_time`` is populated only for terminal states.
    """
    s = job.status
    completion_time = _parse_timestamp(s.completion_time)
    if s.succeeded and s.succeeded > 0:
        return WorkflowStatus.SUCCEEDED, completion_time
    if s.failed and s.failed > 0:
        return WorkflowStatus.FAILED, completion_time
    if s.active and s.active > 0:
        return WorkflowStatus.RUNNING, None
    return WorkflowStatus.PENDING, None


def _read_workflow_metadata(
    k8s_batch, namespace: str, workflow_id: str
) -> tuple[Optional[str], Optional[int], WorkflowStatus, Optional[datetime.datetime], Optional[datetime.datetime]]:
    """Read controller Job → ``(name, total_steps, status, dt_start, dt_end)``.

    Returns ``(None, None, UNKNOWN, None, None)`` if the Job no longer exists (404).
    """
    try:
        job = k8s_batch.read_namespaced_job(name=workflow_id, namespace=namespace)
    except k8s.client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        return None, None, WorkflowStatus.UNKNOWN, None, None

    labels = job.metadata.labels or {}
    annotations = job.metadata.annotations or {}
    name = labels.get("seekr-chain/job-name") or None
    raw_count = annotations.get("seekr-chain/step-count")
    total_steps = int(raw_count) if raw_count else None
    # Use start_time (pod scheduled) to match what ``chain list`` reports.
    # Fall back to creation_timestamp if the pod hasn't started yet.
    dt_start = _parse_timestamp(job.status.start_time) or _parse_timestamp(job.metadata.creation_timestamp)
    status, dt_end = _job_status_and_completion(job)
    return name, total_steps, status, dt_start, dt_end


def _list_jobsets_by_step(k8s_custom, namespace: str, workflow_id: str) -> dict[str, dict]:
    """Return ``{step_name: jobset_dict}`` for all JobSets belonging to this workflow."""
    try:
        jobsets = k8s_custom.list_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            label_selector=f"seekr-chain/job-id={workflow_id}",
        ).get("items", [])
    except Exception:
        jobsets = []
    return {
        js["metadata"]["labels"]["seekr-chain/step-name"]: js
        for js in jobsets
        if "seekr-chain/step-name" in js.get("metadata", {}).get("labels", {})
    }


def _group_pods_by_step_and_role(k8s_v1, namespace: str, workflow_id: str, known_steps) -> dict[str, dict]:
    """Return ``{step_name: {role_name: [pod, ...]}}`` for worker pods of this workflow.

    ``known_steps`` seeds the result so steps with no pods yet still appear
    (e.g. a step suspended by Kueue before any pods exist).
    """
    pods = k8s_v1.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"seekr-chain/job-id={workflow_id},seekr-chain/is-controller!=true",
    ).items
    roles_by_step: dict[str, dict] = {step_name: {} for step_name in known_steps}
    for pod in pods:
        step_name = pod.metadata.labels.get("seekr-chain/step")
        if step_name not in roles_by_step:
            roles_by_step[step_name] = {}
        role = pod.metadata.labels.get("seekr-chain/role")
        roles_by_step[step_name].setdefault(role, []).append(pod)
    return roles_by_step


def get_workflow_state(k8s_custom, k8s_v1, k8s_batch, namespace: str, workflow_id: str) -> WorkflowState:
    """Build a complete ``WorkflowState`` snapshot for the given workflow.

    Reads the controller Job (for workflow-level metadata and status),
    the JobSets (one per step), and the worker pods. The resulting
    ``WorkflowState`` carries everything the rendering layer needs — no
    extra context required from the caller.
    """
    name, total_steps, status, dt_start, dt_end = _read_workflow_metadata(k8s_batch, namespace, workflow_id)
    jobset_by_step = _list_jobsets_by_step(k8s_custom, namespace, workflow_id)
    roles_by_step = _group_pods_by_step_and_role(k8s_v1, namespace, workflow_id, jobset_by_step.keys())
    steps = [
        _collect_step_state(step_name, roles_by_step.get(step_name, {}), js) for step_name, js in jobset_by_step.items()
    ]
    return WorkflowState(
        id=workflow_id,
        name=name,
        status=status,
        dt_start=dt_start,
        dt_end=dt_end,
        total_steps=total_steps,
        captured_at=datetime.datetime.now(tz=datetime.timezone.utc),
        steps=steps,
    )


def get_workflow_job_status(
    k8s_batch, namespace: str, workflow_id: str
) -> tuple[WorkflowStatus, Optional[datetime.datetime]]:
    """Return ``(status, completion_time)`` for the controller Job — lightweight.

    Used by ``K8sWorkflow.get_status()`` (called repeatedly by ``wait()``) so
    callers can poll status without fetching the full workflow state.
    """
    job = k8s_batch.read_namespaced_job_status(name=workflow_id, namespace=namespace)
    return _job_status_and_completion(job)


def first_running_or_finished_pod(workflow_state: WorkflowState) -> Optional[PodState]:
    """Return the first pod in workflow order whose status is running or finished.

    Used by ``attach()`` to pick a pod to ``kubectl exec`` into.
    """
    for step_state in workflow_state.steps:
        for role_state in step_state.roles:
            for pod in role_state.pods:
                if pod.status.is_running() or pod.status.is_finished():
                    return pod
    return None


def is_jobset_suspended(k8s_custom, name: str, namespace: str) -> bool:
    """Return True if the named JobSet has ``spec.suspend=True``, False otherwise."""
    try:
        js = k8s_custom.get_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            name=name,
        )
        return bool(js.get("spec", {}).get("suspend", False))
    except Exception:
        return False
