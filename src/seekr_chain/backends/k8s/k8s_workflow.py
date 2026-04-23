#!/usr/bin/env python3

import datetime
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import boto3
import kubernetes as k8s
from kubernetes.client.rest import ApiException
from rich.console import Console
from rich.text import Text

from seekr_chain import k8s_utils, s3_utils
from seekr_chain.backends.k8s.job_info import JobInfo, get_job_info
from seekr_chain.backends.k8s.parse_logs import LogStore, parse_logs
from seekr_chain.constants import LOCAL_LOG_PATH
from seekr_chain.live import maybe_live
from seekr_chain.render_status import render_compact_pod_status
from seekr_chain.status import ContainerStatus, PodStatus, WorkflowStatus
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


def _parse_timestamp(ts):
    return ts if isinstance(ts, datetime.datetime) else None


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
    dt_start: Optional[datetime.datetime]
    dt_end: Optional[datetime.datetime]
    steps: list[StepState]


_PULL_ERROR_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
_SKIP_WAITING_MESSAGE = {"CrashLoopBackOff", "ContainerCreating", "PodInitializing"}


def _trim_pull_message(message: str) -> str:
    if message.startswith("Back-off "):
        marker = "ErrImagePull: "
        idx = message.find(marker)
        if idx != -1:
            message = message[idx + len(marker):]
    return message


def _collect_container_states(
    container_statuses: list[k8s.client.models.v1_container_status.V1ContainerStatus],
    is_init: bool = False,
) -> list[ContainerState]:
    out = []
    if container_statuses:
        for container_status in container_statuses:
            container_state = ContainerState(
                name=container_status.name,
                dt_start=None,
                dt_end=None,
                status=ContainerStatus("UNKNOWN"),
            )
            if container_status.state.waiting:
                reason = container_status.state.waiting.reason or ""
                if reason in _PULL_ERROR_REASONS:
                    container_state.status = ContainerStatus.PULL_ERROR
                elif is_init:
                    container_state.status = ContainerStatus.INIT_WAITING
                else:
                    container_state.status = ContainerStatus.WAITING
                if reason not in _SKIP_WAITING_MESSAGE:
                    raw = container_status.state.waiting.message or None
                    if raw and reason in _PULL_ERROR_REASONS:
                        raw = _trim_pull_message(raw)
                    container_state.message = raw or None
            elif container_status.state.terminated:
                term = container_status.state.terminated
                if term.exit_code == 0:
                    container_state.status = ContainerStatus.SUCCEEDED
                else:
                    container_state.status = ContainerStatus.INIT_ERROR if is_init else ContainerStatus.FAILED
                    if (term.reason or "") == "OOMKilled":
                        container_state.reason = "OOMKilled"
                container_state.dt_start = _parse_timestamp(term.started_at)
                container_state.dt_end = _parse_timestamp(term.finished_at)
            elif container_status.state.running:
                container_state.status = ContainerStatus.INIT_RUNNING if is_init else ContainerStatus.RUNNING
                container_state.dt_start = _parse_timestamp(container_status.state.running.started_at)
            else:
                raise NotImplementedError(
                    f"Unexpected container state for '{container_status.name}': not waiting, terminated, or running"
                )

            out.append(container_state)

    return out


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

    init_statuses = [c.status for c in pod_state.init_containers]
    main_statuses = [c.status for c in pod_state.containers]
    phase = (pod.status.phase or "UNKNOWN").upper()

    all_statuses = init_statuses + main_statuses

    if phase in ("SUCCEEDED", "FAILED"):
        pod_state.status = PodStatus(phase)
    elif any(s == ContainerStatus.PULL_ERROR for s in all_statuses):
        pod_state.status = PodStatus.PULL_ERROR
    elif any(s == ContainerStatus.RUNNING for s in main_statuses):
        pod_state.status = PodStatus.RUNNING
    elif any(s in (ContainerStatus.SUCCEEDED, ContainerStatus.FAILED) for s in main_statuses):
        # Main containers have terminated but pod phase hasn't updated yet (transient).
        # Show FAILED eagerly; otherwise keep RUNNING until the phase flips to Succeeded.
        if any(s == ContainerStatus.FAILED for s in main_statuses):
            pod_state.status = PodStatus.FAILED
        else:
            pod_state.status = PodStatus.RUNNING
    elif init_statuses:
        if any(s == ContainerStatus.INIT_RUNNING for s in init_statuses):
            pod_state.status = PodStatus.INIT_RUNNING
        elif any(s == ContainerStatus.INIT_ERROR for s in init_statuses):
            pod_state.status = PodStatus.INIT_ERROR
        elif all(s == ContainerStatus.SUCCEEDED for s in init_statuses):
            pod_state.status = PodStatus.PULLING
        else:
            pod_state.status = PodStatus.INIT_WAITING
    elif main_statuses:
        pod_state.status = PodStatus.PULLING
    else:
        pod_state.status = PodStatus.PENDING

    dt_ends = [c.dt_end for c in pod_state.init_containers + pod_state.containers if c.dt_end]
    if dt_ends:
        pod_state.dt_end = max(dt_ends)

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
    start_times = [c.get("lastTransitionTime") for c in conditions if c.get("lastTransitionTime")]
    dt_start = _parse_timestamp(min(start_times)) if start_times else None

    return PodState(
        name=step_name,
        status=pod_status,
        dt_start=dt_start,
        dt_end=None,
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


def _spawn_follow_pod_thread(k8s_v1, name, namespace, step_name, role_name, job_index, container_name=None):
    def _follow():
        prefix = f"{step_name}"
        if role_name:
            prefix += f"-{role_name}"
        prefix += f"-{job_index} | "
        try:
            stream = k8s_v1.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container_name,
                follow=True,
                _preload_content=False,
                timestamps=False,
            )
            for line in stream:
                print(f"{prefix}{line.decode('utf-8').rstrip()}")
        except Exception as e:
            print(f"[ERROR] Following logs from {name}/{container_name}: {e}")

    thread = threading.Thread(target=_follow, daemon=True)
    thread.start()
    return thread


def _should_follow(pod_state: PodState, followed_pods: set, all_replicas: bool = False) -> bool:
    if pod_state.name in followed_pods:
        return False
    if all_replicas is False and pod_state.job_index != 0:
        return False
    if pod_state.status.is_running() or pod_state.status.is_finished():
        return True
    return False


def _first_running_or_finished_pod(workflow_state: WorkflowState) -> PodState | None:
    for step_state in workflow_state.steps:
        for role_state in step_state.roles:
            for pod in role_state.pods:
                if pod.status.is_running() or pod.status.is_finished():
                    return pod
    return None


def _get_k8s_workflow_status(workflow_id: str, namespace: str, k8s_batch: k8s.client.BatchV1Api) -> WorkflowStatus:
    job = k8s_batch.read_namespaced_job_status(name=workflow_id, namespace=namespace)
    status = job.status
    if status.succeeded and status.succeeded > 0:
        return WorkflowStatus.SUCCEEDED
    if status.failed and status.failed > 0:
        return WorkflowStatus.FAILED
    if status.active and status.active > 0:
        return WorkflowStatus.RUNNING
    return WorkflowStatus.PENDING


def _print_interactive_welcome(name):
    splash = r"""
       ________  _____    _____   __
      / ____/ / / /   |  /  _/ | / /
     / /   / /_/ / /| |  / //  |/ /
    / /___/ __  / ___ |_/ // /|  /
    \____/_/ /_/_/  |_/___/_/ |_/
    """

    message = f"""
    Workflow ID: {name}

    Type `c-d` to exit this shell

    To run this job, use `/seekr-chain/resources/chain-entrypoint.sh`
    """

    print(splash + "\n\n" + message)



class K8sWorkflow(Workflow):
    def __init__(self, id, namespace=None, s3_client=None):
        self._id = id
        if s3_client is None:
            s3_client = boto3.client("s3")
        self._s3_client = s3_client

        self._k8s_v1 = k8s_utils.get_core_v1_api()
        self._k8s_batch = k8s.client.BatchV1Api()
        self._k8s_custom = k8s_utils.get_custom_objects_api()

        if namespace is None:
            _, active_ctx = k8s.config.list_kube_config_contexts(config_file=os.environ.get("KUBECONFIG"))
            namespace = active_ctx["context"].get("namespace", "default")
        self._namespace = namespace

        # Get datastore_root from controller Job annotation
        datastore_root = None
        try:
            job = self._k8s_batch.read_namespaced_job(name=self._id, namespace=self._namespace)
            datastore_root = (job.metadata.annotations or {}).get("seekr-chain/datastore-root") or None
        except ApiException as e:
            if e.status != 404:
                raise
        self._job_info: JobInfo = get_job_info(self._id, datastore_root=datastore_root)

    @property
    def name(self):
        return self._id

    @property
    def id(self):
        return self._id

    def get_logs(self, timestamps=False) -> LogStore:
        """Get logs from S3."""
        local_log_path = LOCAL_LOG_PATH / self._id

        logger.debug("Syncing logs")
        s3_utils.download_dir(self._job_info["remote_step_data_path"], local_log_path, self._s3_client, sync=True)

        logger.debug("Expanding logs")
        return parse_logs(local_log_path, timestamps)

    def get_status(self) -> WorkflowStatus:
        return _get_k8s_workflow_status(self._id, self._namespace, self._k8s_batch)

    def get_detailed_state(self) -> WorkflowState:
        """Get detailed per-step/role/pod state for this workflow.

        Drives from JobSets (one per step) so every submitted step appears
        immediately — including those suspended by Kueue before any pods exist.
        Worker pods are joined in to populate the per-pod rows.
        """
        # 1. List all JobSets for this workflow (one per step).
        try:
            jobsets = self._k8s_custom.list_namespaced_custom_object(
                group="jobset.x-k8s.io", version="v1alpha2", plural="jobsets",
                namespace=self._namespace,
                label_selector=f"seekr-chain/job-id={self._id}",
            ).get("items", [])
        except Exception:
            jobsets = []

        # Build a step_name → jobset dict.
        jobset_by_step: dict[str, dict] = {
            js["metadata"]["labels"]["seekr-chain/step-name"]: js
            for js in jobsets
            if "seekr-chain/step-name" in js.get("metadata", {}).get("labels", {})
        }

        # 2. List worker pods and group by step → role.
        pods = self._k8s_v1.list_namespaced_pod(
            namespace=self._namespace,
            label_selector=f"seekr-chain/job-id={self._id},seekr-chain/is-controller!=true",
        ).items

        roles_by_step: dict[str, dict] = {name: {} for name in jobset_by_step}
        for pod in pods:
            step_name = pod.metadata.labels.get("seekr-chain/step")
            if step_name not in roles_by_step:
                roles_by_step[step_name] = {}
            role = pod.metadata.labels.get("seekr-chain/role")
            roles_by_step[step_name].setdefault(role, []).append(pod)

        # 3. Build StepStates — status comes from the JobSet, pods from the pod list.
        steps = [
            _collect_step_state(step_name, roles_by_step.get(step_name, {}), js)
            for step_name, js in jobset_by_step.items()
        ]

        return WorkflowState(dt_start=None, dt_end=None, steps=steps)

    def delete(self):
        """Delete the controller Job, all worker JobSets, and the Secret."""
        # 1. Delete worker JobSets
        try:
            self._k8s_custom.delete_collection_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=self._namespace,
                label_selector=f"seekr-chain/job-id={self._id}",
            )
        except ApiException as e:
            logger.warning(f"Failed to delete JobSets for {self._id}: {e}")

        # 2. Delete controller Job (propagate=Background cascades to the Job's pod)
        try:
            self._k8s_batch.delete_namespaced_job(
                name=self._id,
                namespace=self._namespace,
                body=k8s.client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete Job {self._id}: {e}")

        # 3. Delete the Secret
        try:
            self._k8s_v1.delete_namespaced_secret(name=self._id, namespace=self._namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete Secret {self._id}: {e}")

    @staticmethod
    def format_state(workflow_state: WorkflowState) -> str:
        lines = []
        indent = 13

        for step_state in sorted(workflow_state.steps, key=lambda x: (x.dt_start is None, x.dt_start)):
            role_lines = []
            pod_statuses = []
            for role_state in sorted(step_state.roles, key=lambda x: x.name or ""):
                role_indent = indent

                if len(step_state.roles) > 1:
                    role_indent += 2

                role_pod_lines = []
                role_pod_statuses = []
                for pod_state in sorted(role_state.pods, key=lambda x: x.job_index):
                    role_pod_lines += [f"{' ' * (role_indent + 2)}{pod_state.status.value} : {pod_state.name}"]
                    for c in pod_state.init_containers + pod_state.containers:
                        annotation = c.reason or c.message
                        if annotation:
                            role_pod_lines += [f"{' ' * (role_indent + 4)}{annotation}"]
                            break
                    role_pod_statuses.append(pod_state.status)

                if len(step_state.roles) > 1:
                    role_lines += [
                        f"{' ' * role_indent}{role_state.status.value} : {role_state.name} {render_compact_pod_status(role_pod_statuses)}"
                    ]
                role_lines += role_pod_lines
                pod_statuses += role_pod_statuses

            lines += [
                f"{' ' * indent}{step_state.pod.status.value} : {step_state.name} {render_compact_pod_status(pod_statuses)}"
            ] + role_lines

        return "\n".join(lines)

    def follow(self, plain=False, all_replicas=False):
        followed_pods = set()
        follow_threads = []
        console = Console()

        with maybe_live(plain=plain, console=console, refresh_per_second=4, transient=False) as live:
            while True:
                status = self.get_status()
                workflow_state = self.get_detailed_state()

                status_text = f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] {status.value} : {self._id}"
                status_text += f"\n{self.format_state(workflow_state)}"

                live.update(Text(status_text))

                if status.is_finished():
                    break

                for step_state in workflow_state.steps:
                    for role_state in step_state.roles:
                        for pod_state in role_state.pods:
                            if _should_follow(pod_state, followed_pods, all_replicas=all_replicas):
                                followed_pods.add(pod_state.name)
                                follow_threads.append(
                                    _spawn_follow_pod_thread(
                                        self._k8s_v1,
                                        pod_state.name,
                                        self._namespace,
                                        container_name="main",
                                        step_name=step_state.name,
                                        role_name=role_state.name,
                                        job_index=pod_state.job_index,
                                    )
                                )

                time.sleep(1)

            for t_thread in follow_threads:
                t_thread.join(timeout=2)

    def attach(self):
        """Attach to an interactive job."""
        logger.info(f"Waiting for job to start {self.name}")

        console = Console()
        plain = False
        poll_interval = 1
        with maybe_live(plain=plain, console=console, refresh_per_second=4, transient=False) as live:
            while True:
                status = self.get_status()
                workflow_state = self.get_detailed_state()

                status_text = f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] {status.value} : {self._id}"
                status_text += f"\n{self.format_state(workflow_state)}"

                live.update(Text(status_text))

                pod = _first_running_or_finished_pod(workflow_state)
                if pod is not None:
                    print(f"First running/finished pod: {pod.name}")
                    break

                time.sleep(poll_interval)

        assert isinstance(pod, PodState)

        if pod.status.is_running():
            if shutil.which("kubectl") is None:
                raise RuntimeError(
                    "kubectl not found in PATH.\n\n"
                    "Install kubectl to use interactive mode:\n"
                    "  https://kubernetes.io/docs/tasks/tools/install-kubectl/"
                )

            logger.info("Connecting")

            _print_interactive_welcome(self.name)
            subprocess.run(["kubectl", "exec", "-it", pod.name, "--", "/bin/bash"])

            print(
                "\n\nDisconnected\n\nThis workflow will continue to run until terminated! To terminate this job, run:"
            )
            print(f"\n  chain delete {self.name}\n")
        else:
            print(f"Error connecting to pod, status: {pod.status.value}")
