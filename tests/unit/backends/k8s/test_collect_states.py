"""
Unit tests for _collect_container_states(), _collect_pod_state(), and is_jobset_suspended().

Uses types.SimpleNamespace to build minimal fake K8s objects.
"""

import datetime
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s.workflow_state import (
    _collect_container_states,
    _collect_pod_state,
    _resolve_status,
    _trim_pull_message,
    is_jobset_suspended,
)
from seekr_chain.status import ContainerStatus, PodStatus

UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Builders for fake K8s objects
# ---------------------------------------------------------------------------


def _waiting(reason=None, message=None):
    return SimpleNamespace(
        waiting=SimpleNamespace(reason=reason, message=message),
        terminated=None,
        running=None,
    )


def _running(started_at=None):
    return SimpleNamespace(
        waiting=None,
        terminated=None,
        running=SimpleNamespace(started_at=started_at),
    )


def _terminated(exit_code=0, reason=None, started_at=None, finished_at=None):
    return SimpleNamespace(
        waiting=None,
        terminated=SimpleNamespace(exit_code=exit_code, reason=reason, started_at=started_at, finished_at=finished_at),
        running=None,
    )


def _container(name="c", state=None):
    return SimpleNamespace(name=name, state=state)


def _pod(phase="Running", init_containers=None, containers=None, labels=None, start_time=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="fake-pod-0",
            labels={
                "jobset.sigs.k8s.io/job-index": "0",
                "jobset.sigs.k8s.io/job-global-index": "0",
                "jobset.sigs.k8s.io/restart-attempt": "0",
                **(labels or {}),
            },
        ),
        status=SimpleNamespace(
            phase=phase,
            start_time=start_time,
            init_container_statuses=init_containers,
            container_statuses=containers,
        ),
    )


# ---------------------------------------------------------------------------
# _collect_container_states — main containers (is_init=False)
# ---------------------------------------------------------------------------


class TestCollectContainerStatesMain:
    def test_waiting_no_reason(self):
        states = _collect_container_states([_container(state=_waiting())], is_init=False)
        assert states[0].status == ContainerStatus.WAITING

    @pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"])
    def test_pull_error_reasons(self, reason):
        states = _collect_container_states([_container(state=_waiting(reason=reason))], is_init=False)
        assert states[0].status == ContainerStatus.PULL_ERROR

    def test_running(self):
        states = _collect_container_states([_container(state=_running())], is_init=False)
        assert states[0].status == ContainerStatus.RUNNING

    def test_terminated_success(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=0))], is_init=False)
        assert states[0].status == ContainerStatus.SUCCEEDED

    def test_terminated_failure(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=1))], is_init=False)
        assert states[0].status == ContainerStatus.FAILED

    def test_empty_list(self):
        assert _collect_container_states([], is_init=False) == []

    def test_none_list(self):
        assert _collect_container_states(None, is_init=False) == []

    def test_unknown_state_returns_unknown(self):
        # K8s guarantees one of waiting/running/terminated is always set (protobuf oneof),
        # so this path should never be reached in practice. We degrade to UNKNOWN rather
        # than crashing chain status.
        bad = SimpleNamespace(name="c", state=SimpleNamespace(waiting=None, terminated=None, running=None))
        states = _collect_container_states([bad], is_init=False)
        assert states[0].status == ContainerStatus.UNKNOWN


# ---------------------------------------------------------------------------
# _collect_container_states — init containers (is_init=True)
# ---------------------------------------------------------------------------


class TestCollectContainerStatesInit:
    def test_waiting_no_reason(self):
        states = _collect_container_states([_container(state=_waiting())], is_init=True)
        assert states[0].status == ContainerStatus.INIT_WAITING

    @pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"])
    def test_pull_error_reasons(self, reason):
        states = _collect_container_states([_container(state=_waiting(reason=reason))], is_init=True)
        assert states[0].status == ContainerStatus.PULL_ERROR

    def test_running(self):
        states = _collect_container_states([_container(state=_running())], is_init=True)
        assert states[0].status == ContainerStatus.INIT_RUNNING

    def test_terminated_success(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=0))], is_init=True)
        assert states[0].status == ContainerStatus.SUCCEEDED

    def test_terminated_failure(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=1))], is_init=True)
        assert states[0].status == ContainerStatus.INIT_ERROR


# ---------------------------------------------------------------------------
# _collect_pod_state — pod status derivation
# ---------------------------------------------------------------------------


class TestCollectPodState:
    # --- terminal phase short-circuits ---

    def test_phase_succeeded(self):
        pod = _pod(phase="Succeeded", containers=[_container(state=_terminated(exit_code=0))])
        assert _collect_pod_state(pod).status == PodStatus.SUCCEEDED

    def test_phase_failed(self):
        pod = _pod(phase="Failed", containers=[_container(state=_terminated(exit_code=1))])
        assert _collect_pod_state(pod).status == PodStatus.FAILED

    # --- main container running ---

    def test_running_main_container(self):
        pod = _pod(containers=[_container(state=_running())])
        assert _collect_pod_state(pod).status == PodStatus.RUNNING

    def test_running_wins_over_pending_init(self):
        """If main container is running, pod is RUNNING even if init containers still show waiting."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_running())],
        )
        assert _collect_pod_state(pod).status == PodStatus.RUNNING

    def test_pull_error_wins_over_running_sidecar(self):
        """PULL:ERROR on one container takes priority even when a sidecar is running."""
        pod = _pod(
            containers=[
                _container("main", state=_waiting(reason="ImagePullBackOff")),
                _container("sidecar", state=_running()),
            ],
        )
        assert _collect_pod_state(pod).status == PodStatus.PULL_ERROR

    # --- init container states ---

    def test_init_running(self):
        pod = _pod(
            init_containers=[_container("i", state=_running())],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.INIT_RUNNING

    def test_init_error(self):
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=1))],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.INIT_ERROR

    def test_init_pull_error(self):
        pod = _pod(
            init_containers=[_container("i", state=_waiting(reason="ImagePullBackOff"))],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.PULL_ERROR

    def test_init_waiting_not_started(self):
        """Init containers scheduled but in waiting state with no pull error → INIT:WAITING."""
        pod = _pod(
            init_containers=[_container("i", state=_waiting())],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.INIT_WAITING

    # --- after init: pulling main image ---

    def test_pulling_after_init(self):
        """All init containers done, main container waiting with no pull error → PULLING."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.PULLING

    def test_pull_error_after_init(self):
        """All init containers done, main container pull failing → PULL:ERROR."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_waiting(reason="ImagePullBackOff"))],
        )
        assert _collect_pod_state(pod).status == PodStatus.PULL_ERROR

    def test_pull_error_main_no_init(self):
        """No init containers, main container pull failing → PULL:ERROR."""
        pod = _pod(containers=[_container("c", state=_waiting(reason="ErrImagePull"))])
        assert _collect_pod_state(pod).status == PodStatus.PULL_ERROR

    def test_pulling_no_init(self):
        """No init containers, main container waiting (no error) → PULLING."""
        pod = _pod(containers=[_container("c", state=_waiting())])
        assert _collect_pod_state(pod).status == PodStatus.PULLING

    # --- no containers yet (pod not scheduled) ---

    def test_pending_no_containers(self):
        pod = _pod(init_containers=None, containers=None)
        assert _collect_pod_state(pod).status == PodStatus.PENDING

    def test_pending_empty_containers(self):
        pod = _pod(init_containers=[], containers=[])
        assert _collect_pod_state(pod).status == PodStatus.PENDING

    # --- metadata is plumbed through ---

    def test_metadata_labels(self):
        pod = _pod(
            containers=[_container(state=_running())],
            labels={
                "jobset.sigs.k8s.io/job-index": "2",
                "jobset.sigs.k8s.io/job-global-index": "5",
                "jobset.sigs.k8s.io/restart-attempt": "1",
            },
        )
        state = _collect_pod_state(pod)
        assert state.job_index == 2
        assert state.job_global_index == 5
        assert state.restart_attempt == 1

    def test_multiple_init_containers_partial_done(self):
        """Mix of succeeded + still-running init containers → INIT:RUNNING."""
        pod = _pod(
            init_containers=[
                _container("i0", state=_terminated(exit_code=0)),
                _container("i1", state=_running()),
            ],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.INIT_RUNNING

    def test_pull_error_priority_over_init_waiting(self):
        """PULL:ERROR takes priority over other init container states."""
        pod = _pod(
            init_containers=[
                _container("i0", state=_waiting(reason="ImagePullBackOff")),
                _container("i1", state=_waiting()),
            ],
            containers=[_container("c", state=_waiting())],
        )
        assert _collect_pod_state(pod).status == PodStatus.PULL_ERROR


# ---------------------------------------------------------------------------
# ContainerState.message and .reason fields
# ---------------------------------------------------------------------------


class TestContainerStateAnnotations:
    def test_pull_error_message_populated(self):
        msg = "docker.io/library/ubuntu:99.99: not found"
        states = _collect_container_states(
            [_container(state=_waiting(reason="ErrImagePull", message=msg))], is_init=False
        )
        assert states[0].message == msg

    def test_waiting_no_message_is_none(self):
        states = _collect_container_states([_container(state=_waiting())], is_init=False)
        assert states[0].message is None

    def test_crashloopbackoff_message_suppressed(self):
        """CrashLoopBackOff message is redundant noise — should not be surfaced."""
        states = _collect_container_states(
            [
                _container(
                    state=_waiting(reason="CrashLoopBackOff", message="back-off 5m0s restarting failed container")
                )
            ],
            is_init=False,
        )
        assert states[0].message is None

    def test_container_creating_message_suppressed(self):
        states = _collect_container_states(
            [_container(state=_waiting(reason="ContainerCreating", message="some transient message"))],
            is_init=False,
        )
        assert states[0].message is None

    def test_create_container_config_error_message_shown(self):
        msg = 'secret "my-secret" not found'
        states = _collect_container_states(
            [_container(state=_waiting(reason="CreateContainerConfigError", message=msg))], is_init=False
        )
        assert states[0].message == msg

    def test_oom_killed_reason_populated(self):
        states = _collect_container_states(
            [_container(state=_terminated(exit_code=137, reason="OOMKilled"))], is_init=False
        )
        assert states[0].reason == "OOMKilled"
        assert states[0].status == ContainerStatus.FAILED

    def test_non_oom_terminated_reason_not_populated(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=1, reason="Error"))], is_init=False)
        assert states[0].reason is None

    def test_successful_terminated_no_reason(self):
        states = _collect_container_states(
            [_container(state=_terminated(exit_code=0, reason="Completed"))], is_init=False
        )
        assert states[0].reason is None

    def test_oom_killed_in_init_container(self):
        """OOMKilled should also be captured for init containers."""
        states = _collect_container_states(
            [_container(state=_terminated(exit_code=137, reason="OOMKilled"))], is_init=True
        )
        assert states[0].reason == "OOMKilled"
        assert states[0].status == ContainerStatus.INIT_ERROR

    def test_pull_error_message_trimmed(self):
        """ImagePullBackOff messages have kubelet boilerplate stripped."""
        raw = (
            'Back-off pulling image "harbor.example.com/rocm/pytorch:bad-tag": '
            "ErrImagePull: initializing source docker://harbor.example.com/rocm/pytorch:bad-tag: "
            "reading manifest bad-tag in harbor.example.com/rocm/pytorch: unknown: resource not found"
        )
        states = _collect_container_states(
            [_container(state=_waiting(reason="ImagePullBackOff", message=raw))], is_init=False
        )
        msg = states[0].message
        assert msg is not None
        assert not msg.startswith("Back-off")
        assert "resource not found" in msg

    def test_non_pull_error_message_not_trimmed(self):
        """Messages for non-pull errors are shown as-is."""
        raw = 'secret "my-secret" not found'
        states = _collect_container_states(
            [_container(state=_waiting(reason="CreateContainerConfigError", message=raw))], is_init=False
        )
        assert states[0].message == raw


class TestTrimPullMessage:
    def test_trims_backoff_prefix(self):
        raw = 'Back-off pulling image "img:tag": ErrImagePull: reading manifest tag: not found'
        assert _trim_pull_message(raw) == "reading manifest tag: not found"

    def test_no_backoff_prefix_unchanged(self):
        raw = "some other message"
        assert _trim_pull_message(raw) == raw

    def test_backoff_without_errimagepull_marker_unchanged(self):
        """If the expected marker isn't there, return the original rather than losing info."""
        raw = "Back-off pulling image: unexpected format"
        assert _trim_pull_message(raw) == raw


# ---------------------------------------------------------------------------
# is_jobset_suspended
# ---------------------------------------------------------------------------


class _FakeCustomApi:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def get_namespaced_custom_object(self, **kwargs):
        if self._exc:
            raise self._exc
        return self._response


class TestIsJobsetSuspended:
    def test_suspended_true(self):
        api = _FakeCustomApi({"spec": {"suspend": True}})
        assert is_jobset_suspended(api, "my-jobset", "argo-workflows") is True

    def test_suspended_false(self):
        api = _FakeCustomApi({"spec": {"suspend": False}})
        assert is_jobset_suspended(api, "my-jobset", "argo-workflows") is False

    def test_suspend_field_absent(self):
        api = _FakeCustomApi({"spec": {}})
        assert is_jobset_suspended(api, "my-jobset", "argo-workflows") is False

    def test_api_exception_returns_false(self):
        api = _FakeCustomApi(exc=ApiException(status=404))
        assert is_jobset_suspended(api, "missing-jobset", "argo-workflows") is False

    def test_unexpected_exception_returns_false(self):
        api = _FakeCustomApi(exc=RuntimeError("unexpected"))
        assert is_jobset_suspended(api, "my-jobset", "argo-workflows") is False


# ---------------------------------------------------------------------------
# _collect_pod_state — time-semantics
# ---------------------------------------------------------------------------


class TestCollectPodStateTimeSemantics:
    """Lock in the pod time-derivation rules:

    - Before main containers start (INIT:*, PULLING), dt_start = pod.start_time.
    - Once any main container has started running, dt_start resets to the
      earliest main-container start time so the duration measures "work
      runtime", not pod lifetime.
    - dt_end only finalizes when the pod itself is terminal — never for
      active pods (regression for the original bug: PULLING pods froze at
      the init container's finished_at timestamp).
    """

    POD_START = "2026-01-01T12:00:00Z"
    INIT_START = "2026-01-01T12:00:01Z"
    INIT_END = "2026-01-01T12:00:05Z"
    MAIN_START = "2026-01-01T12:00:10Z"
    MAIN_END = "2026-01-01T12:00:30Z"

    def _ts(self, s: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))

    def test_pulling_pod_dt_end_not_frozen_at_init_finish(self):
        """Regression: PULLING pod with completed init must NOT inherit init's dt_end."""
        pod = _pod(
            start_time=self.POD_START,
            init_containers=[_container("i", state=_terminated(started_at=self.INIT_START, finished_at=self.INIT_END))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == PodStatus.PULLING
        assert state.dt_end is None  # would equal self._ts(INIT_END) without the fix

    def test_pulling_pod_dt_start_is_pod_start_time(self):
        """Before main starts, duration counts from pod scheduling."""
        pod = _pod(
            start_time=self.POD_START,
            init_containers=[_container("i", state=_terminated(started_at=self.INIT_START, finished_at=self.INIT_END))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.dt_start == self._ts(self.POD_START)

    def test_init_running_dt_end_none_dt_start_is_pod(self):
        pod = _pod(
            start_time=self.POD_START,
            init_containers=[_container("i", state=_running(started_at=self.INIT_START))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == PodStatus.INIT_RUNNING
        assert state.dt_end is None
        assert state.dt_start == self._ts(self.POD_START)

    def test_running_pod_resets_dt_start_to_main_container_start(self):
        """Once main starts, dt_start = min(main container starts), not pod.start_time."""
        pod = _pod(
            phase="Running",
            start_time=self.POD_START,
            init_containers=[_container("i", state=_terminated(started_at=self.INIT_START, finished_at=self.INIT_END))],
            containers=[_container("c", state=_running(started_at=self.MAIN_START))],
        )
        state = _collect_pod_state(pod)
        assert state.status == PodStatus.RUNNING
        assert state.dt_start == self._ts(self.MAIN_START)
        assert state.dt_end is None  # main still running

    def test_running_pod_uses_earliest_main_start(self):
        early = "2026-01-01T12:00:10Z"
        later = "2026-01-01T12:00:15Z"
        pod = _pod(
            phase="Running",
            start_time=self.POD_START,
            containers=[
                _container("c0", state=_running(started_at=later)),
                _container("c1", state=_running(started_at=early)),
            ],
        )
        state = _collect_pod_state(pod)
        assert state.dt_start == self._ts(early)

    def test_succeeded_pod_dt_end_is_max_main_end(self):
        """Terminal pod with main containers → dt_end = max(main ends), not init ends."""
        pod = _pod(
            phase="Succeeded",
            start_time=self.POD_START,
            init_containers=[_container("i", state=_terminated(started_at=self.INIT_START, finished_at=self.INIT_END))],
            containers=[
                _container("c", state=_terminated(exit_code=0, started_at=self.MAIN_START, finished_at=self.MAIN_END))
            ],
        )
        state = _collect_pod_state(pod)
        assert state.status == PodStatus.SUCCEEDED
        assert state.dt_start == self._ts(self.MAIN_START)
        assert state.dt_end == self._ts(self.MAIN_END)

    def test_init_error_pod_falls_back_to_all_container_ends(self):
        """When main never started (init failure), dt_end falls back to all-container max."""
        pod = _pod(
            phase="Failed",
            start_time=self.POD_START,
            init_containers=[
                _container("i", state=_terminated(exit_code=1, started_at=self.INIT_START, finished_at=self.INIT_END))
            ],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == PodStatus.FAILED
        # main never ran, so dt_start stays at pod.start_time
        assert state.dt_start == self._ts(self.POD_START)
        # dt_end falls back to init's finish time
        assert state.dt_end == self._ts(self.INIT_END)


# ---------------------------------------------------------------------------
# _resolve_status
# ---------------------------------------------------------------------------


class TestResolveStatus:
    """``_resolve_status`` applies (container_status → pod_status) rules in
    order. Two-tuple = match if ANY container has the status. Three-tuple
    with ``"all"`` = match only if ALL containers have the status."""

    def test_any_match_returns_first_hit(self):
        result = _resolve_status(
            [ContainerStatus.RUNNING, ContainerStatus.WAITING],
            [
                (ContainerStatus.RUNNING, PodStatus.RUNNING),
                (ContainerStatus.SUCCEEDED, PodStatus.SUCCEEDED),
            ],
        )
        assert result == PodStatus.RUNNING

    def test_rules_evaluated_in_order(self):
        """First matching rule wins, even when later rules would also match."""
        result = _resolve_status(
            [ContainerStatus.FAILED, ContainerStatus.SUCCEEDED],
            [
                (ContainerStatus.FAILED, PodStatus.FAILED),
                (ContainerStatus.SUCCEEDED, PodStatus.SUCCEEDED),
            ],
        )
        assert result == PodStatus.FAILED

    def test_no_match_returns_none(self):
        result = _resolve_status(
            [ContainerStatus.WAITING],
            [(ContainerStatus.RUNNING, PodStatus.RUNNING)],
        )
        assert result is None

    def test_empty_statuses_returns_none(self):
        result = _resolve_status([], [(ContainerStatus.RUNNING, PodStatus.RUNNING)])
        assert result is None

    def test_all_match_requires_every_container(self):
        """The ``"all"`` form: rule matches only when EVERY container has the status."""
        all_succeeded = [ContainerStatus.SUCCEEDED, ContainerStatus.SUCCEEDED]
        mixed = [ContainerStatus.SUCCEEDED, ContainerStatus.WAITING]
        rule = [(ContainerStatus.SUCCEEDED, PodStatus.PULLING, "all")]
        assert _resolve_status(all_succeeded, rule) == PodStatus.PULLING
        assert _resolve_status(mixed, rule) is None
