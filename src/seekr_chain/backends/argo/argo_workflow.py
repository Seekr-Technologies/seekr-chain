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
from seekr_chain.backends.argo.job_info import JobInfo, get_job_info
from seekr_chain.backends.argo.parse_logs import LogStore, parse_logs
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
    # name: Optional[str]
    # status: PodStatus
    steps: list[StepState]


_PULL_ERROR_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
# Waiting reasons whose message is redundant or transient — don't surface them
_SKIP_WAITING_MESSAGE = {"CrashLoopBackOff", "ContainerCreating", "PodInitializing"}


def _trim_pull_message(message: str) -> str:
    """
    Strip kubelet boilerplate from image-pull waiting messages.

    Raw form:
      Back-off pulling image "img": ErrImagePull: initializing source docker://img: ...actual error...
    We want just:
      ...actual error...
    """
    # Drop the "Back-off pulling image ..." prefix up to the first colon-space
    if message.startswith("Back-off "):
        # Find "ErrImagePull: " and take everything after it
        marker = "ErrImagePull: "
        idx = message.find(marker)
        if idx != -1:
            message = message[idx + len(marker) :]
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
        # Pull errors take priority — a failing container overrides running sidecars
        pod_state.status = PodStatus.PULL_ERROR
    elif any(s == ContainerStatus.RUNNING for s in main_statuses):
        pod_state.status = PodStatus.RUNNING
    elif init_statuses:
        if any(s == ContainerStatus.INIT_RUNNING for s in init_statuses):
            pod_state.status = PodStatus.INIT_RUNNING
        elif any(s == ContainerStatus.INIT_ERROR for s in init_statuses):
            pod_state.status = PodStatus.INIT_ERROR
        elif all(s == ContainerStatus.SUCCEEDED for s in init_statuses):
            pod_state.status = PodStatus.PULLING
        else:
            # Scheduled but init containers haven't started yet
            pod_state.status = PodStatus.INIT_WAITING
    elif main_statuses:
        # No init containers
        pod_state.status = PodStatus.PULLING
    else:
        # No containers reported yet — pod not yet scheduled
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

    # Set start/end times based on min/max start/end times of pods
    dt_starts = [pod.dt_start for pod in out.pods if pod.dt_start]
    if dt_starts:
        out.dt_start = min(dt_starts)
    dt_ends = [pod.dt_end for pod in out.pods if pod.dt_end]
    if dt_ends:
        out.dt_end = max(dt_ends)

    # Set status based on pods
    out.status = min([pod.status for pod in out.pods])

    return out


def _collect_step_state(name, roles, pod) -> StepState:
    step_state = StepState(
        dt_start=None,
        dt_end=None,
        name=name,
        roles=[_collect_role_state(role_name, role_pods) for role_name, role_pods in roles.items()],
        pod=_collect_pod_state(pod),
    )
    return step_state


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
                # tail_lines=-1,
            )
            for line in stream:
                print(f"{prefix}{line.decode('utf-8').rstrip()}")

        except Exception as e:
            print(f"[ERROR] Following logs from {name}/{container_name}: {e}")

    thread = threading.Thread(target=_follow, daemon=True)
    thread.start()
    return thread


def _parse_jobset_pod_name(name: str) -> dict | None:
    if match := re.match(r"^(?P<jobset>.+)-(?P<job_index>\d+)-(?P<pod_index>\d+)-(?P<suffix>[a-z0-9]+)$", name):
        out = match.groupdict()
        out["pod_index"] = int(out["pod_index"])
        out["job_index"] = int(out["job_index"])
        return out
    else:
        return None


def _print_interactive_welcome(name):
    splash = r"""
       ________  _____    _____   __
      / ____/ / / /   |  /  _/ | / /
     / /   / /_/ / /| |  / //  |/ /
    / /___/ __  / ___ |_/ // /|  /
    \____/_/ /_/_/  |_/___/_/ |_/
    """

    message = f"""
    Argo Workflow Name: {name}

    Type `c-d` to exit this shell

    To run this job, use `/seekr-chain/entrypoint.sh`
    """

    print(splash + "\n\n" + message)


def _should_follow(pod_state: PodState, followed_pods: set, all_replicas: bool = False) -> bool:
    """
    Helper to determine if we should follow this pod
    """
    if pod_state.name in followed_pods:
        return False
    if all_replicas is False and pod_state.job_index != 0:
        return False
    if pod_state.status.is_running() or pod_state.status.is_finished():
        return True

    return False


def _first_running_or_finished_pod(workflow_state: WorkflowState) -> PodState | None:
    """
    Get the first running or finished pod from a WorkflowState
    """
    for step_state in workflow_state.steps:
        for role_state in step_state.roles:
            for pod in role_state.pods:
                if pod.status.is_running() or pod.status.is_finished():
                    return pod
    return None


def _is_jobset_suspended(k8s_custom: k8s.client.CustomObjectsApi, jobset_name: str, namespace: str) -> bool:
    """Return True if the JobSet exists and spec.suspend is True (i.e. queued, not yet admitted)."""
    try:
        jobset = k8s_custom.get_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=namespace,
            name=jobset_name,
        )
    except Exception:
        return False
    return jobset.get("spec", {}).get("suspend", False)


def _get_workflow_status(workflow_name: str, namespace: str, k8s_custom: k8s.client.CustomObjectsApi) -> WorkflowStatus:
    workflow = k8s_custom.get_namespaced_custom_object(
        group="argoproj.io",
        version="v1alpha1",
        plural="workflows",
        namespace=namespace,
        name=workflow_name,
    )
    status_str = workflow.get("status", {}).get("phase", "Unknown")
    return WorkflowStatus(status_str.upper())


class ArgoWorkflow(Workflow):
    def __init__(self, id, namespace=None, s3_client=None):
        self._id = id
        if s3_client is None:
            s3_client = boto3.client("s3")
        self._s3_client = s3_client

        self._k8s_v1 = k8s_utils.get_core_v1_api()
        self._k8s_custom = k8s_utils.get_custom_objects_api()

        if namespace is None:
            _, active_ctx = k8s.config.list_kube_config_contexts(config_file=os.environ.get("KUBECONFIG"))
            namespace = active_ctx["context"].get("namespace", "default")
        self._namespace = namespace

        datastore_root = None
        try:
            workflow_obj = self._k8s_custom.get_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                plural="workflows",
                namespace=self._namespace,
                name=self._id,
            )
            datastore_root = workflow_obj["metadata"]["annotations"].get("seekr-chain/datastore-root")
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
        """
        Get logs
        """
        local_log_path = LOCAL_LOG_PATH / self._id

        logger.debug("Syncing logs")
        s3_utils.download_dir(self._job_info["remote_step_data_path"], local_log_path, self._s3_client, sync=True)

        logger.debug("Expanding logs")
        return parse_logs(local_log_path, timestamps)

    def get_logs_from_k8s(self, init_containers=False, system_steps=False, as_list=False):
        """
        Legacy log retrieval, using k8s
        """
        argo_label_selector = f"workflows.argoproj.io/workflow={self._id}"
        pods = self._k8s_v1.list_namespaced_pod(namespace=self._namespace, label_selector=argo_label_selector).items

        pod_logs = {}
        for pod in pods:
            pod_name = pod.metadata.name
            is_system_pod = pod.metadata.labels.get("seekr-chain/system-pod", "").lower() == "true"
            is_jobset_pod = pod.metadata.labels.get("seekr-chain/is-jobset", "").lower() == "true"
            step_name = pod.metadata.labels.get("seekr-chain/step-name", pod_name)

            if system_steps is False and is_system_pod is True:
                continue

            pod_logs[step_name] = {
                "pod_name": pod_name,
                "type": "system" if is_system_pod else "user",
                "is_jobset": is_jobset_pod,
            }

            if is_jobset_pod:
                if system_steps:
                    pod_logs[step_name]["sys_logs"] = k8s_utils.get_container_logs(
                        v1_api=self._k8s_v1, pod=pod, namespace=self._namespace, as_list=as_list
                    )
            else:
                pod_logs[step_name]["logs"] = k8s_utils.get_container_logs(
                    v1_api=self._k8s_v1, pod=pod, namespace=self._namespace, as_list=as_list
                )

            if init_containers and pod.spec.init_containers:
                pod_logs[step_name]["init_containers"] = {}
                for pod_init_container in pod.spec.init_containers:
                    pod_logs[step_name]["init_containers"][pod_init_container.name] = k8s_utils.get_container_logs(
                        v1_api=self._k8s_v1,
                        pod=pod,
                        namespace=self._namespace,
                        as_list=as_list,
                        container_name=pod_init_container.name,
                    )

            if is_jobset_pod:
                # We should set this info on the argo pod itself
                jobset_label_selector = (
                    f"jobset.sigs.k8s.io/jobset-name={pod.metadata.labels.get('seekr-chain/jobset-name')}"
                )

                jobset_pods = self._k8s_v1.list_namespaced_pod(
                    namespace=self._namespace, label_selector=jobset_label_selector
                ).items

                js_data = {}

                for js_pod in jobset_pods:
                    js_pod_index = int(js_pod.metadata.labels.get("jobset.sigs.k8s.io/job-global-index"))
                    js_pod_name = js_pod.metadata.name

                    js_data[js_pod_index] = {
                        "logs": k8s_utils.get_container_logs(
                            v1_api=self._k8s_v1, pod=js_pod, namespace=self._namespace, as_list=as_list
                        ),
                        "pod_name": js_pod_name,
                    }

                    if init_containers and js_pod.spec.init_containers:
                        js_data[js_pod_index]["init_containers"] = {}
                        for js_init_cont in js_pod.spec.init_containers:
                            js_data[js_pod_index]["init_containers"][js_init_cont.name] = k8s_utils.get_container_logs(
                                v1_api=self._k8s_v1,
                                pod=js_pod,
                                namespace=self._namespace,
                                as_list=as_list,
                                container_name=js_init_cont.name,
                            )

                pod_logs[step_name]["jobset"] = js_data

        return pod_logs

    def get_status(self) -> WorkflowStatus:
        return _get_workflow_status(self._id, self._namespace, self._k8s_custom)

    def get_detailed_state(self) -> WorkflowState:
        """Get detailed per-step/role/pod state for this workflow."""
        pods = self._k8s_v1.list_namespaced_pod(
            namespace=self._namespace, label_selector=f"seekr-chain/job-id={self._id}"
        ).items

        pod_hierarchy = {}
        for pod in pods:
            step_name = pod.metadata.labels.get("seekr-chain/step")
            if step_name not in pod_hierarchy:
                pod_hierarchy[step_name] = {"roles": {}}

            if pod.metadata.labels.get("seekr-chain/is-step-pod"):
                pod_hierarchy[step_name]["pod"] = pod
            else:
                role = pod.metadata.labels.get("seekr-chain/role")
                if role not in pod_hierarchy[step_name]["roles"]:
                    pod_hierarchy[step_name]["roles"][role] = []
                pod_hierarchy[step_name]["roles"][role].append(pod)

        steps = []
        for step_name, step_data in pod_hierarchy.items():
            step_state = _collect_step_state(step_name, step_data["roles"], step_data["pod"])
            if not step_state.roles:
                jobset_name = step_data["pod"].metadata.labels.get("seekr-chain/jobset-name")
                if jobset_name and _is_jobset_suspended(self._k8s_custom, jobset_name, self._namespace):
                    step_state.pod.status = PodStatus.PENDING
            steps.append(step_state)

        return WorkflowState(dt_start=None, dt_end=None, steps=steps)

    def delete(self):
        """Delete the Argo workflow from the cluster."""
        self._k8s_custom.delete_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            plural="workflows",
            namespace=self._namespace,
            name=self._id,
        )

    def cancel(self):
        """Stop the Argo workflow without deleting it."""
        self._k8s_custom.patch_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            plural="workflows",
            namespace=self._namespace,
            name=self._id,
            body={"spec": {"shutdown": "Terminate"}},
        )

    @staticmethod
    def format_state(workflow_state: WorkflowState) -> str:
        lines = []
        indent = 13

        for step_state in sorted(workflow_state.steps, key=lambda x: (x.dt_start is None, x.dt_start)):
            role_lines = []
            pod_statuses = []
            for role_state in sorted(step_state.roles, key=lambda x: x.name or ""):
                role_indent = indent

                # Only print roles if we have >1 role
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

                detailed_status = True
                if detailed_status:
                    status_text += f"\n{self.format_state(workflow_state)}"

                live.update(Text(status_text))

                # Don't break until we update the console
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
        """
        Attach to an interactive job
        """
        logger.info(f"Waiting for job to start {self.name}")

        # TODO: CONSOLIDATE CODE WITH FOLLOW!!
        console = Console()
        plain = False
        poll_interval = 1
        with maybe_live(plain=plain, console=console, refresh_per_second=4, transient=False) as live:
            while True:
                status = self.get_status()
                workflow_state = self.get_detailed_state()

                status_text = f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] {status.value} : {self._id}"

                detailed_status = True
                if detailed_status:
                    status_text += f"\n{self.format_state(workflow_state)}"

                live.update(Text(status_text))
                # Break then the first pod is running or finished

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
