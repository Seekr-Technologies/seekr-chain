#!/usr/bin/env python3

import codecs
import logging
import shutil
import subprocess
import threading

import kubernetes as k8s
from kubernetes.client.rest import ApiException
from rich.console import Console

from seekr_chain import remote_fs
from seekr_chain.backends.k8s.job_info import JobInfo, get_job_info
from seekr_chain.backends.k8s.parse_logs import LogStore, parse_logs
from seekr_chain.backends.k8s.render_status import format_plain, render
from seekr_chain.backends.k8s.watched_state import (
    WatchStalledError,
    controller_status_watcher,
    print_reconnect_hint,
    workflow_state_watcher,
)
from seekr_chain.backends.k8s.workflow_state import (
    PodState,
    WorkflowState,
    first_running_or_finished_pod,
    get_workflow_job_status,
    get_workflow_state,
    read_controller_jobset,
)
from seekr_chain.constants import LOCAL_LOG_PATH
from seekr_chain.k8s_api import kube
from seekr_chain.live import maybe_live
from seekr_chain.status import WorkflowStatus
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Log-following helpers (used by follow())
# ---------------------------------------------------------------------------


def _spawn_follow_pod_thread(k8s_v1, name, namespace, step_name, role_name, job_index, container_name=None):
    def _follow():
        prefix = f"{step_name}-{role_name}-{job_index} | "
        try:
            stream = k8s_v1.read_namespaced_pod_log(
                name=name,
                namespace=namespace,
                container=container_name,
                follow=True,
                _preload_content=False,
                timestamps=False,
            )
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            for chunk in stream:
                text = decoder.decode(chunk)
                if text:
                    for line in text.splitlines():
                        print(f"{prefix}{line}")
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


# ---------------------------------------------------------------------------
# K8sWorkflow
# ---------------------------------------------------------------------------


class K8sWorkflow(Workflow):
    def __init__(self, id, namespace=None):
        self._id = id

        self._k8s_v1 = kube.core_v1
        self._k8s_custom = kube.custom_objects

        self._namespace = namespace if namespace is not None else kube.namespace

        # Read datastore_root from the controller JobSet annotation. All other
        # workflow-level metadata (name, status, timing, step count) is
        # populated on each ``WorkflowState`` snapshot rather than cached here.
        controller_jobset = read_controller_jobset(self._k8s_custom, self._namespace, self._id)
        datastore_root = None
        if controller_jobset is not None:
            annotations = controller_jobset.get("metadata", {}).get("annotations", {}) or {}
            datastore_root = annotations.get("seekr-chain/datastore-root") or None
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
        remote_fs.download(self._job_info["remote_step_data_path"], local_log_path)

        logger.debug("Expanding logs")
        return parse_logs(local_log_path, timestamps)

    def get_status(self) -> WorkflowStatus:
        status, _ = get_workflow_job_status(self._k8s_custom, self._k8s_v1, self._namespace, self._id)
        return status

    def watch_controller_status(self):
        """Yield WorkflowStatus on each change by watching only the controller JobSet.

        Deliberately separate from workflow_state_watcher() (see watched_state.py),
        which watches the controller JobSet *and* worker JobSets *and* Pods to
        build a full per-step/pod WorkflowState for the live follow()/attach()
        display. This method only needs the controller JobSet's own status,
        and wait() may be watching many jobs concurrently — one lightweight
        watch per job keeps that cheap rather than paying for 3x the watches
        and list calls per job. Built on controller_status_watcher(), which
        shares its reconnect/backoff bookkeeping with workflow_state_watcher().
        """
        try:
            with controller_status_watcher(self._k8s_custom, self._k8s_v1, self._namespace, self._id) as watcher:
                last_status = None
                status = watcher.wait_for_first()
                while True:
                    if status is None:
                        # Controller JobSet is gone — end the stream so callers
                        # (e.g. wait._watch_to_completion) fall through to
                        # their get_status()-based fallback instead of hanging.
                        return
                    if status != last_status:
                        yield status
                        last_status = status
                    if status.is_finished():
                        return
                    watcher.wait_for_update()
                    status = watcher.latest()
        except WatchStalledError as e:
            print_reconnect_hint(e)
            raise

    def get_detailed_state(self) -> WorkflowState:
        return get_workflow_state(self._k8s_custom, self._k8s_v1, self._namespace, self._id)

    def format_state(self, workflow_state: WorkflowState) -> str:
        """Plain-text tabular rendering for CLI use."""
        return format_plain(workflow_state)

    def delete(self):
        """Delete the controller JobSet, all worker JobSets, and the Secret."""
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

        # 2. Delete controller JobSet (propagate=Background cascades to its pod)
        try:
            self._k8s_custom.delete_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=self._namespace,
                name=self._id,
                body=k8s.client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete controller JobSet {self._id}: {e}")

        # 3. Delete the Secret
        try:
            self._k8s_v1.delete_namespaced_secret(name=self._id, namespace=self._namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning(f"Failed to delete Secret {self._id}: {e}")

    def cancel(self):
        """Suspend all worker JobSets without deleting them."""
        try:
            jobsets = self._k8s_custom.list_namespaced_custom_object(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=self._namespace,
                label_selector=f"seekr-chain/job-id={self._id},seekr-chain/is-controller!=true",
            ).get("items", [])
        except ApiException as e:
            logger.warning(f"Failed to list JobSets for {self._id}: {e}")
            return

        for js in jobsets:
            name = js["metadata"]["name"]
            try:
                self._k8s_custom.patch_namespaced_custom_object(
                    group="jobset.x-k8s.io",
                    version="v1alpha2",
                    plural="jobsets",
                    namespace=self._namespace,
                    name=name,
                    body={"spec": {"suspend": True}},
                )
            except ApiException as e:
                logger.warning(f"Failed to suspend JobSet {name}: {e}")

    def follow(self, plain=False, all_replicas=False):
        followed_pods = set()
        follow_threads = []
        console = Console()

        try:
            with (
                workflow_state_watcher(self._k8s_custom, self._k8s_v1, self._namespace, self._id) as watcher,
                maybe_live(plain=plain, console=console, refresh_per_second=4, transient=False) as live,
            ):
                workflow_state = watcher.wait_for_first()
                while True:
                    live.update(render(workflow_state, watcher.connection_status()))

                    if workflow_state.status.is_finished() or workflow_state.status == WorkflowStatus.UNKNOWN:
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

                    # Wake up as soon as any watched resource (JobSet/Pod) changes,
                    # falling back to a 1 s timeout in case a change was missed.
                    watcher.wait_for_update(timeout=1.0)
                    workflow_state = watcher.latest()

                for t_thread in follow_threads:
                    t_thread.join(timeout=2)
        except WatchStalledError as e:
            # Printed only once the Live display above has stopped and released
            # the terminal — printing while it's still rendering interleaves
            # into mangled output.
            print_reconnect_hint(e)
            raise

    def attach(self):
        """Attach to an interactive job."""
        logger.info(f"Waiting for job to start {self.name}")

        console = Console()
        plain = False
        try:
            with (
                workflow_state_watcher(self._k8s_custom, self._k8s_v1, self._namespace, self._id) as watcher,
                maybe_live(plain=plain, console=console, refresh_per_second=4, transient=False) as live,
            ):
                workflow_state = watcher.wait_for_first()
                while True:
                    live.update(render(workflow_state, watcher.connection_status()))

                    pod = first_running_or_finished_pod(workflow_state)
                    if pod is not None:
                        print(f"First running/finished pod: {pod.name}")
                        break

                    watcher.wait_for_update(timeout=1.0)
                    workflow_state = watcher.latest()
        except WatchStalledError as e:
            print_reconnect_hint(e)
            raise

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
