"""
Unit tests for ReconnectingWatcher, via its workflow_state_watcher() and
controller_status_watcher() factories.

Mirrors test_state_fetcher.py's mocking style, but instead of faking a
fetch_fn, fakes the Kubernetes Watch API: kubernetes.watch.Watch().stream()
is patched to replay a scripted list of events per resource kind (job,
jobset, or pod — classified by the call's distinguishing kwargs, see
_stream_kind()). Both the controller JobSet ("job") and worker JobSets
("jobset") go through CustomObjectsApi and pass group= in their kwargs;
they're told apart by the controller's is-controller=true label selector.
"""

import time
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s import watched_state as watched_state_mod
from seekr_chain.backends.k8s.watched_state import (
    ReconnectingWatcher,
    WatchStalledError,
    controller_status_watcher,
    workflow_state_watcher,
)
from seekr_chain.status import WorkflowStatus

# ---------------------------------------------------------------------------
# Fake Kubernetes Watch API
# ---------------------------------------------------------------------------


def _stream_kind(kwargs):
    """Classify a Watch().stream() call by its kwargs — bound methods get a
    fresh id() on every attribute access, so we can't key off the func
    argument's identity. The controller-JobSet and worker-JobSet watches both
    pass group=, told apart by the is-controller=true label selector; the pod
    watch passes neither.
    """
    if "group" in kwargs:
        if "is-controller=true" in kwargs.get("label_selector", ""):
            return "job"
        return "jobset"
    return "pod"


def make_fake_watch_class(events_by_kind):
    """Return a fake replacement for kubernetes.watch.Watch.

    ``events_by_kind``: {"job"|"jobset"|"pod": [ [event, ...], [event, ...], ... ]}
    Each inner list is replayed on successive calls to stream() for that
    resource kind — the first call gets round 0, the second round 1, etc.
    Once a kind's rounds are exhausted, stream() sleeps briefly and returns
    an empty generator (avoids a hot spin loop while the test finishes up).
    """
    call_counts: dict = {}

    class FakeWatch:
        def stream(self, func, **kwargs):
            kind = _stream_kind(kwargs)
            idx = call_counts.get(kind, 0)
            call_counts[kind] = idx + 1
            rounds = events_by_kind.get(kind, [])
            if idx < len(rounds):
                yield from rounds[idx]
            else:
                time.sleep(0.02)
                return

        def stop(self):
            pass

    return FakeWatch


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _controller_jobset(resource_version="1", active=0, terminal_state=None, labels=None):
    status = {}
    if active:
        status["replicatedJobsStatus"] = [{"active": active}]
    if terminal_state:
        status["terminalState"] = terminal_state
    return {
        "metadata": {
            "name": "wf-1",
            "resourceVersion": resource_version,
            "labels": labels or {},
            "annotations": {},
        },
        "status": status,
    }


def _pod(name, step, resource_version="1"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            resource_version=resource_version,
            labels={
                "seekr-chain/step": step,
                "seekr-chain/role": None,
                "jobset.sigs.k8s.io/job-index": "0",
                "jobset.sigs.k8s.io/job-global-index": "0",
                "jobset.sigs.k8s.io/restart-attempt": "0",
            },
        ),
        status=SimpleNamespace(phase="Pending", start_time=None, init_container_statuses=None, container_statuses=None),
    )


def _jobset_dict(name, step_name, resource_version="1"):
    return {
        "metadata": {"name": name, "labels": {"seekr-chain/step-name": step_name}, "resourceVersion": resource_version},
        "spec": {},
        "status": {},
    }


class FakeK8sCustom:
    def __init__(self, controller_jobset, jobsets=None):
        self._controller_jobset = controller_jobset
        self._jobsets = jobsets or []

    def get_namespaced_custom_object(self, **kwargs):
        if self._controller_jobset is None:
            raise ApiException(status=404)
        return self._controller_jobset

    def list_namespaced_custom_object(self, **kwargs):
        return {"items": self._jobsets, "metadata": {"resourceVersion": "1"}}


class FakeK8sV1:
    def __init__(self, pods):
        self._pods = pods

    def list_namespaced_pod(self, **kwargs):
        return SimpleNamespace(items=self._pods, metadata=SimpleNamespace(resource_version="1"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_seed_produces_first_snapshot_without_waiting_for_watch_events(monkeypatch):
    controller_jobset = _controller_jobset(active=1, labels={"seekr-chain/job-name": "my-job"})
    k8s_custom = FakeK8sCustom(controller_jobset)
    k8s_v1 = FakeK8sV1([])

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1") as w:
        state = w.wait_for_first(timeout=1)
        assert state.name == "my-job"
        assert state.status == WorkflowStatus.RUNNING


def _wait_until(predicate, watcher, timeout=1.0):
    """Poll watcher.latest() until predicate(state) holds or timeout elapses.

    The seed snapshot and the scripted watch event can race — a watcher
    thread may apply its event before the test even calls wait_for_first()
    — so tests assert on eventual state rather than a specific before/after
    ordering relative to wait_for_first().
    """
    deadline = time.monotonic() + timeout
    state = watcher.latest()
    while not predicate(state):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"condition not met within {timeout}s; last state: {state}")
        watcher.wait_for_update(timeout=remaining)
        state = watcher.latest()
    return state


def test_job_modified_event_updates_status(monkeypatch):
    jobset_v1 = _controller_jobset(resource_version="1", active=1)
    jobset_v2 = _controller_jobset(resource_version="2", terminal_state="Completed")
    k8s_custom = FakeK8sCustom(jobset_v1)
    k8s_v1 = FakeK8sV1([])

    fake_watch_cls = make_fake_watch_class(
        {
            "job": [[{"type": "MODIFIED", "object": jobset_v2}]],
        }
    )
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        state = _wait_until(lambda s: s.status == WorkflowStatus.SUCCEEDED, w)
        assert state.status == WorkflowStatus.SUCCEEDED


def test_pod_added_event_appears_in_next_snapshot(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    jobsets = [_jobset_dict("wf-1-step-a", "step-a")]
    k8s_custom = FakeK8sCustom(controller_jobset, jobsets)
    k8s_v1 = FakeK8sV1([])

    new_pod = _pod("wf-1-step-a-0", "step-a")
    fake_watch_cls = make_fake_watch_class(
        {
            "pod": [[{"type": "ADDED", "object": new_pod}]],
        }
    )
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        state = _wait_until(lambda s: s.steps[0].roles != [], w)
        assert [p.name for p in state.steps[0].roles[0].pods] == ["wf-1-step-a-0"]


def test_pod_deleted_event_removes_pod_from_next_snapshot(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    existing_pod = _pod("wf-1-step-a-0", "step-a")
    jobsets = [_jobset_dict("wf-1-step-a", "step-a")]
    k8s_custom = FakeK8sCustom(controller_jobset, jobsets)
    k8s_v1 = FakeK8sV1([existing_pod])

    fake_watch_cls = make_fake_watch_class(
        {
            "pod": [[{"type": "DELETED", "object": existing_pod}]],
        }
    )
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        state = _wait_until(lambda s: s.steps[0].roles == [], w)
        assert state.steps[0].roles == []


def test_wait_for_update_times_out_when_nothing_changes(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)
    k8s_v1 = FakeK8sV1([])

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        assert w.wait_for_update(timeout=0.05) is False


def test_stop_joins_watcher_threads_promptly(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)
    k8s_v1 = FakeK8sV1([])

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    w = workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1")
    w.start()
    w.wait_for_first(timeout=1)
    w.stop(join_timeout=1.0)
    assert all(not t.is_alive() for t in w._threads)


# ---------------------------------------------------------------------------
# Error escalation
# ---------------------------------------------------------------------------


class _FailingWatch:
    """Fake Watch() whose job stream always raises; jobset/pod streams idle."""

    def stream(self, func, **kwargs):
        if _stream_kind(kwargs) == "job":
            raise ApiException(status=401, reason="Unauthorized")
        time.sleep(0.02)
        return iter(())

    def stop(self):
        pass


def test_continuous_watch_failures_escalate_to_watch_stalled_error(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)
    k8s_v1 = FakeK8sV1([])

    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", _FailingWatch)
    monkeypatch.setattr(watched_state_mod, "_WATCH_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(watched_state_mod, "_WATCH_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(watched_state_mod, "_WATCH_BACKOFF_MAX_SECONDS", 0.01)

    with workflow_state_watcher(k8s_custom, k8s_v1, "ns", "wf-1", max_attempts=3) as w:
        w.wait_for_first(timeout=1)
        with pytest.raises(WatchStalledError, match="job watch failed after 3 attempts.*401 Unauthorized"):
            w.wait_for_update(timeout=2.0)

    # stop() (via __exit__) must return promptly even though watchers are stopped mid-failure.
    assert all(not t.is_alive() for t in w._threads)


def test_success_resets_failure_streak():
    """A single success in between failures should reset the attempt count, so
    a retry limit longer than any individual failure streak is never hit.

    Exercises _record_failure/_record_success directly (no watch threads
    started) since this is purely about the streak-tracking bookkeeping.
    """
    w = ReconnectingWatcher(label="wf-1", specs=[], project=lambda caches: None, max_attempts=2)

    w._record_failure("job", ApiException(status=401, reason="Unauthorized"))
    w._record_success("job")
    w._record_failure("job", ApiException(status=401, reason="Unauthorized"))
    assert w._fatal_error is None


# ---------------------------------------------------------------------------
# controller_status_watcher()
# ---------------------------------------------------------------------------


def _wait_until_status(predicate, watcher, timeout=1.0):
    """Same shape as _wait_until, but for a controller_status_watcher()'s
    latest() (a bare WorkflowStatus, not a WorkflowState)."""
    deadline = time.monotonic() + timeout
    status = watcher.latest()
    while not predicate(status):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"condition not met within {timeout}s; last status: {status}")
        watcher.wait_for_update(timeout=remaining)
        status = watcher.latest()
    return status


def test_controller_seed_produces_first_status_without_waiting_for_watch_events(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with controller_status_watcher(k8s_custom, "ns", "wf-1") as w:
        status = w.wait_for_first(timeout=1)
        assert status == WorkflowStatus.RUNNING


def test_controller_returns_none_when_job_does_not_exist(monkeypatch):
    k8s_custom = FakeK8sCustom(None)

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with controller_status_watcher(k8s_custom, "ns", "wf-1") as w:
        status = w.wait_for_first(timeout=1)
        assert status is None


def test_controller_job_modified_event_updates_status(monkeypatch):
    jobset_v1 = _controller_jobset(resource_version="1", active=1)
    jobset_v2 = _controller_jobset(resource_version="2", terminal_state="Completed")
    k8s_custom = FakeK8sCustom(jobset_v1)

    fake_watch_cls = make_fake_watch_class(
        {
            "job": [[{"type": "MODIFIED", "object": jobset_v2}]],
        }
    )
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with controller_status_watcher(k8s_custom, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        status = _wait_until_status(lambda s: s == WorkflowStatus.SUCCEEDED, w)
        assert status == WorkflowStatus.SUCCEEDED


def test_controller_deleted_event_clears_status(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)

    fake_watch_cls = make_fake_watch_class(
        {
            "job": [[{"type": "DELETED", "object": controller_jobset}]],
        }
    )
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    with controller_status_watcher(k8s_custom, "ns", "wf-1") as w:
        w.wait_for_first(timeout=1)
        status = _wait_until_status(lambda s: s is None, w)
        assert status is None


def test_controller_stop_joins_watcher_thread_promptly(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)

    fake_watch_cls = make_fake_watch_class({})
    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", fake_watch_cls)

    w = controller_status_watcher(k8s_custom, "ns", "wf-1")
    w.start()
    w.wait_for_first(timeout=1)
    w.stop(join_timeout=1.0)
    assert all(not t.is_alive() for t in w._threads)


def test_controller_continuous_watch_failures_escalate_to_watch_stalled_error(monkeypatch):
    controller_jobset = _controller_jobset(active=1)
    k8s_custom = FakeK8sCustom(controller_jobset)

    monkeypatch.setattr(watched_state_mod.k8s.watch, "Watch", _FailingWatch)
    monkeypatch.setattr(watched_state_mod, "_WATCH_RECONNECT_DELAY", 0.01)
    monkeypatch.setattr(watched_state_mod, "_WATCH_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setattr(watched_state_mod, "_WATCH_BACKOFF_MAX_SECONDS", 0.01)

    with controller_status_watcher(k8s_custom, "ns", "wf-1", max_attempts=3) as w:
        w.wait_for_first(timeout=1)
        with pytest.raises(WatchStalledError, match="job watch failed after 3 attempts.*401 Unauthorized"):
            w.wait_for_update(timeout=2.0)

    assert all(not t.is_alive() for t in w._threads)
