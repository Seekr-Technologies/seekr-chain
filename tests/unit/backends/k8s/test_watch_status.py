"""
Unit tests for K8sWorkflow.watch_controller_status() and get_workflow_job_status().

watch_controller_status() delegates to controller_status_watcher() (watched_state.py),
which list-then-watches: a synchronous seed read via get_namespaced_custom_object(),
then a background thread replaying kubernetes.watch.Watch().stream() events.
Mirrors test_watched_state.py's mocking style. K8sWorkflow is constructed via
object.__new__ to skip __init__'s cluster/S3 setup, since watch_controller_status() only
touches self._k8s_custom/_namespace/_id.
"""

import time

from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s import k8s_workflow as k8s_workflow_mod
from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow
from seekr_chain.backends.k8s.workflow_state import get_workflow_job_status
from seekr_chain.status import WorkflowStatus


def _jobset(resource_version="1", active=0, terminal_state=None):
    status = {}
    if active:
        status["replicatedJobsStatus"] = [{"active": active}]
    if terminal_state:
        status["terminalState"] = terminal_state
    return {
        "metadata": {"name": "wf-1", "resourceVersion": resource_version},
        "status": status,
    }


def _make_workflow(k8s_custom):
    workflow = object.__new__(K8sWorkflow)
    workflow._k8s_custom = k8s_custom
    workflow._namespace = "ns"
    workflow._id = "wf-1"
    return workflow


class FakeWatch:
    """A small startup delay before the first event mirrors real watch
    latency and keeps the seeded status observable via wait_for_first()
    before the background thread applies any events — without it, the
    watch thread can race ahead of the main thread's very first read.
    """

    def __init__(self, events):
        self._events = events

    def stream(self, func, **kwargs):
        time.sleep(0.05)
        yield from self._events

    def stop(self):
        pass


class FakeK8sCustom:
    def __init__(self, jobset=None):
        self._jobset = jobset

    def get_namespaced_custom_object(self, **kwargs):
        if self._jobset is None:
            raise ApiException(status=404)
        return self._jobset

    def list_namespaced_custom_object(self, **kwargs):
        raise AssertionError("should never be called directly — only used as a Watch() function reference")


def test_watch_controller_status_returns_immediately_when_job_already_gone(monkeypatch):
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch([]))

    workflow = _make_workflow(FakeK8sCustom(jobset=None))
    statuses = list(workflow.watch_controller_status())

    assert statuses == []


def test_watch_controller_status_ends_stream_on_deleted_event(monkeypatch):
    jobset = _jobset(active=1)
    events = [{"type": "DELETED", "object": jobset}]
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch(events))

    workflow = _make_workflow(FakeK8sCustom(jobset=jobset))
    statuses = list(workflow.watch_controller_status())

    assert statuses == [WorkflowStatus.RUNNING]


def test_watch_controller_status_yields_on_change_then_stops_when_finished(monkeypatch):
    jobset_running = _jobset(resource_version="1", active=1)
    jobset_succeeded = _jobset(resource_version="2", terminal_state="Completed")
    events = [
        {"type": "MODIFIED", "object": jobset_running},
        {"type": "MODIFIED", "object": jobset_succeeded},
    ]
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch(events))

    workflow = _make_workflow(FakeK8sCustom(jobset=jobset_running))
    statuses = list(workflow.watch_controller_status())

    assert statuses == [WorkflowStatus.RUNNING, WorkflowStatus.SUCCEEDED]


def test_get_workflow_job_status_returns_unknown_on_404():
    class NotFoundK8sCustom:
        def get_namespaced_custom_object_status(self, **kwargs):
            raise ApiException(status=404)

    status, completion_time = get_workflow_job_status(NotFoundK8sCustom(), "ns", "wf-1")

    assert status == WorkflowStatus.UNKNOWN
    assert completion_time is None


def test_get_workflow_job_status_reraises_non_404_errors():
    class BrokenK8sCustom:
        def get_namespaced_custom_object_status(self, **kwargs):
            raise ApiException(status=500)

    try:
        get_workflow_job_status(BrokenK8sCustom(), "ns", "wf-1")
    except ApiException as e:
        assert e.status == 500
    else:
        raise AssertionError("expected ApiException to propagate")
