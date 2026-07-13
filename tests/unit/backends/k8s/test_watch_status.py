"""
Unit tests for K8sWorkflow.watch_controller_status() and get_workflow_job_status().

watch_controller_status() delegates to controller_status_watcher() (watched_state.py),
which list-then-watches: a synchronous seed read via read_namespaced_job(),
then a background thread replaying kubernetes.watch.Watch().stream() events.
Mirrors test_watched_state.py's mocking style. K8sWorkflow is constructed via
object.__new__ to skip __init__'s cluster/S3 setup, since watch_controller_status() only
touches self._k8s_batch/_namespace/_id.
"""

import time
from types import SimpleNamespace

import kubernetes as k8s
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s import k8s_workflow as k8s_workflow_mod
from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow
from seekr_chain.backends.k8s.workflow_state import get_workflow_job_status
from seekr_chain.status import WorkflowStatus


def _job(resource_version="1", succeeded=0, failed=0, active=0):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="wf-1", resource_version=resource_version),
        status=SimpleNamespace(succeeded=succeeded, failed=failed, active=active, completion_time=None),
    )


def _make_workflow(k8s_batch):
    workflow = object.__new__(K8sWorkflow)
    workflow._k8s_batch = k8s_batch
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


class FakeK8sBatch:
    def __init__(self, job=None):
        self._job = job

    def read_namespaced_job(self, name, namespace):
        if self._job is None:
            raise ApiException(status=404)
        return self._job

    def list_namespaced_job(self, **kwargs):
        raise AssertionError("should never be called directly — only used as a Watch() function reference")


def test_watch_controller_status_returns_immediately_when_job_already_gone(monkeypatch):
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch([]))

    workflow = _make_workflow(FakeK8sBatch(job=None))
    statuses = list(workflow.watch_controller_status())

    assert statuses == []


def test_watch_controller_status_ends_stream_on_deleted_event(monkeypatch):
    job = _job(active=1)
    events = [{"type": "DELETED", "object": job}]
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch(events))

    workflow = _make_workflow(FakeK8sBatch(job=job))
    statuses = list(workflow.watch_controller_status())

    assert statuses == [WorkflowStatus.RUNNING]


def test_watch_controller_status_yields_on_change_then_stops_when_finished(monkeypatch):
    job_running = _job(resource_version="1", active=1)
    job_succeeded = _job(resource_version="2", succeeded=1)
    events = [
        {"type": "MODIFIED", "object": job_running},
        {"type": "MODIFIED", "object": job_succeeded},
    ]
    monkeypatch.setattr(k8s_workflow_mod.k8s.watch, "Watch", lambda: FakeWatch(events))

    workflow = _make_workflow(FakeK8sBatch(job=job_running))
    statuses = list(workflow.watch_controller_status())

    assert statuses == [WorkflowStatus.RUNNING, WorkflowStatus.SUCCEEDED]


def test_get_workflow_job_status_returns_unknown_on_404():
    class NotFoundK8sBatch:
        def read_namespaced_job_status(self, name, namespace):
            raise k8s.client.exceptions.ApiException(status=404)

    status, completion_time = get_workflow_job_status(NotFoundK8sBatch(), "ns", "wf-1")

    assert status == WorkflowStatus.UNKNOWN
    assert completion_time is None


def test_get_workflow_job_status_reraises_non_404_errors():
    class BrokenK8sBatch:
        def read_namespaced_job_status(self, name, namespace):
            raise ApiException(status=500)

    try:
        get_workflow_job_status(BrokenK8sBatch(), "ns", "wf-1")
    except ApiException as e:
        assert e.status == 500
    else:
        raise AssertionError("expected ApiException to propagate")
