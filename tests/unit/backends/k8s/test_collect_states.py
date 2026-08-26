"""
Unit tests for _collect_container_states(), _collect_pod_state(), and is_jobset_suspended().

Uses types.SimpleNamespace to build minimal fake K8s objects.
"""

import datetime
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s.workflow_state import (
    Detail,
    _collect_container_states,
    _collect_pod_state,
    _group_jobsets_by_step,
    _trim_pull_message,
    controller_jobset_status_and_completion,
    get_workflow_job_status,
    is_jobset_suspended,
    list_jobsets,
    read_phases_configmap,
    workflow_cancelled,
    workflow_failed,
)
from seekr_chain.status_model import Status

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
        assert states[0].status == Status.STARTING
        assert states[0].detail is None

    @pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"])
    def test_pull_error_reasons(self, reason):
        states = _collect_container_states([_container(state=_waiting(reason=reason))], is_init=False)
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.PULL_ERROR

    def test_running(self):
        states = _collect_container_states([_container(state=_running())], is_init=False)
        assert states[0].status == Status.RUNNING

    def test_terminated_success(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=0))], is_init=False)
        assert states[0].status == Status.SUCCEEDED

    def test_terminated_failure(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=1))], is_init=False)
        assert states[0].status == Status.FAILED

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
        assert states[0].status == Status.UNKNOWN


# ---------------------------------------------------------------------------
# _collect_container_states — init containers (is_init=True)
# ---------------------------------------------------------------------------


class TestCollectContainerStatesInit:
    def test_waiting_no_reason(self):
        states = _collect_container_states([_container(state=_waiting())], is_init=True)
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.INIT_WAITING

    @pytest.mark.parametrize("reason", ["ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"])
    def test_pull_error_reasons(self, reason):
        states = _collect_container_states([_container(state=_waiting(reason=reason))], is_init=True)
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.PULL_ERROR

    def test_running(self):
        states = _collect_container_states([_container(state=_running())], is_init=True)
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.INIT_RUNNING

    def test_terminated_success(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=0))], is_init=True)
        assert states[0].status == Status.SUCCEEDED

    def test_terminated_failure(self):
        states = _collect_container_states([_container(state=_terminated(exit_code=1))], is_init=True)
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.INIT_ERROR


# ---------------------------------------------------------------------------
# _collect_pod_state — pod status derivation
# ---------------------------------------------------------------------------


class TestCollectPodState:
    # --- terminal phase short-circuits ---

    def test_phase_succeeded(self):
        pod = _pod(phase="Succeeded", containers=[_container(state=_terminated(exit_code=0))])
        assert _collect_pod_state(pod).status == Status.SUCCEEDED

    def test_phase_failed(self):
        pod = _pod(phase="Failed", containers=[_container(state=_terminated(exit_code=1))])
        assert _collect_pod_state(pod).status == Status.FAILED

    # --- main container running ---

    def test_running_main_container(self):
        pod = _pod(containers=[_container(state=_running())])
        assert _collect_pod_state(pod).status == Status.RUNNING

    def test_running_wins_over_pending_init(self):
        """If main container is running, pod is RUNNING even if init containers still show waiting."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_running())],
        )
        assert _collect_pod_state(pod).status == Status.RUNNING

    def test_pull_error_wins_over_running_sidecar(self):
        """PULL:ERROR on one container takes priority even when a sidecar is running."""
        pod = _pod(
            containers=[
                _container("main", state=_waiting(reason="ImagePullBackOff")),
                _container("sidecar", state=_running()),
            ],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULL_ERROR

    # --- init container states ---

    def test_init_running(self):
        pod = _pod(
            init_containers=[_container("i", state=_running())],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.INIT_RUNNING

    def test_chain_nix_init_running_reports_pulling_closure(self):
        """When the running init container is `chain-nix-init`, the pod surfaces
        as PULLING_CLOSURE — the user is waiting on a (potentially multi-GB)
        nix closure fetch, not on generic init work.
        """
        pod = _pod(
            init_containers=[_container("chain-nix-init", state=_running())],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING_CLOSURE

    def test_chain_nix_init_alongside_generic_init(self):
        """chain-nix-init still wins the PULLING_CLOSURE label even when it's
        running concurrently with (or after) a completed generic init.
        """
        pod = _pod(
            init_containers=[
                _container("chain-init", state=_terminated(exit_code=0)),
                _container("chain-nix-init", state=_running()),
            ],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING_CLOSURE

    def test_chain_nix_init_terminated_does_not_report_pulling_closure(self):
        """Once chain-nix-init has finished, we're back to the normal
        derivation (all init done → PULLING for the main image).
        """
        pod = _pod(
            init_containers=[_container("chain-nix-init", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING

    def test_init_error(self):
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=1))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.INIT_ERROR

    def test_init_pull_error(self):
        pod = _pod(
            init_containers=[_container("i", state=_waiting(reason="ImagePullBackOff"))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULL_ERROR

    def test_init_waiting_not_started(self):
        """Init containers scheduled but in waiting state with no pull error → INIT:WAITING."""
        pod = _pod(
            init_containers=[_container("i", state=_waiting())],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.INIT_WAITING

    # --- after init: pulling main image ---

    def test_pulling_after_init(self):
        """All init containers done, main container waiting with no pull error → PULLING."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING

    def test_pull_error_after_init(self):
        """All init containers done, main container pull failing → PULL:ERROR."""
        pod = _pod(
            init_containers=[_container("i", state=_terminated(exit_code=0))],
            containers=[_container("c", state=_waiting(reason="ImagePullBackOff"))],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULL_ERROR

    def test_pull_error_main_no_init(self):
        """No init containers, main container pull failing → PULL:ERROR."""
        pod = _pod(containers=[_container("c", state=_waiting(reason="ErrImagePull"))])
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULL_ERROR

    def test_pulling_no_init(self):
        """No init containers, main container waiting (no error) → PULLING."""
        pod = _pod(containers=[_container("c", state=_waiting())])
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING

    # --- no containers yet (pod not scheduled) ---

    def test_pending_no_containers(self):
        pod = _pod(init_containers=None, containers=None)
        state = _collect_pod_state(pod)
        assert state.status == Status.PENDING
        assert state.detail is None

    def test_pending_empty_containers(self):
        pod = _pod(init_containers=[], containers=[])
        state = _collect_pod_state(pod)
        assert state.status == Status.PENDING
        assert state.detail is None

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
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.INIT_RUNNING

    def test_pull_error_priority_over_init_waiting(self):
        """PULL:ERROR takes priority over other init container states."""
        pod = _pod(
            init_containers=[
                _container("i0", state=_waiting(reason="ImagePullBackOff")),
                _container("i1", state=_waiting()),
            ],
            containers=[_container("c", state=_waiting())],
        )
        state = _collect_pod_state(pod)
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULL_ERROR


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
        assert states[0].status == Status.FAILED

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
        assert states[0].status == Status.STARTING
        assert states[0].detail == Detail.INIT_ERROR

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

    def list_namespaced_custom_object(self, **kwargs):
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

    def test_non_404_api_exception_raises(self):
        """Non-404 API errors (e.g. 403 RBAC) must propagate, not be swallowed."""
        api = _FakeCustomApi(exc=ApiException(status=403))
        with pytest.raises(ApiException):
            is_jobset_suspended(api, "my-jobset", "argo-workflows")

    def test_unexpected_exception_raises(self):
        """Non-Api exceptions must propagate, not be silently swallowed."""
        api = _FakeCustomApi(exc=RuntimeError("unexpected"))
        with pytest.raises(RuntimeError):
            is_jobset_suspended(api, "my-jobset", "argo-workflows")


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
        assert state.status == Status.STARTING
        assert state.detail == Detail.PULLING
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
        assert state.status == Status.STARTING
        assert state.detail == Detail.INIT_RUNNING
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
        assert state.status == Status.RUNNING
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
        assert state.status == Status.SUCCEEDED
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
        assert state.status == Status.FAILED
        # main never ran, so dt_start stays at pod.start_time
        assert state.dt_start == self._ts(self.POD_START)
        # dt_end falls back to init's finish time
        assert state.dt_end == self._ts(self.INIT_END)


# ---------------------------------------------------------------------------
# list_jobsets / _group_jobsets_by_step
# ---------------------------------------------------------------------------


class TestGroupJobsetsByStep:
    def test_returns_jobsets_keyed_by_step_name(self):
        jobsets = [
            {"metadata": {"labels": {"seekr-chain/step-name": "a"}}, "spec": {}, "status": {}},
            {"metadata": {"labels": {"seekr-chain/step-name": "b"}}, "spec": {}, "status": {}},
        ]
        result = _group_jobsets_by_step(jobsets)
        assert set(result.keys()) == {"a", "b"}

    def test_jobset_without_step_name_label_is_skipped(self):
        jobsets = [{"metadata": {"labels": {}}, "spec": {}, "status": {}}]
        assert _group_jobsets_by_step(jobsets) == {}


class TestListJobsets:
    def test_404_propagates(self):
        """A 404 must propagate rather than be swallowed into an empty list —
        the caller (get_workflow_state / the watch seed) needs to distinguish
        "no JobSets yet" from "we couldn't reach the API"."""
        api = _FakeCustomApi(exc=ApiException(status=404))
        with pytest.raises(ApiException):
            list_jobsets(api, "ns", "wf-abc")

    def test_non_404_api_exception_raises(self):
        """Non-404 API errors (e.g. 403 RBAC) must propagate, not return empty."""
        api = _FakeCustomApi(exc=ApiException(status=403))
        with pytest.raises(ApiException):
            list_jobsets(api, "ns", "wf-abc")


# ---------------------------------------------------------------------------
# get_workflow_job_status
# ---------------------------------------------------------------------------


class _FakeCustomStatusApi:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def get_namespaced_custom_object_status(self, **kwargs):
        if self._exc:
            raise self._exc
        return self._response


class _FakeV1NoConfigMap:
    def read_namespaced_config_map(self, **kwargs):
        raise ApiException(status=404)


class TestGetWorkflowJobStatus:
    def test_404_returns_unknown(self):
        """After ttlSecondsAfterFinished deletes the JobSet, status should be UNKNOWN."""
        api = _FakeCustomStatusApi(exc=ApiException(status=404))
        status, dt = get_workflow_job_status(api, _FakeV1NoConfigMap(), "ns", "wf-abc")
        assert status == Status.UNKNOWN
        assert dt is None

    def test_non_404_api_exception_raises(self):
        api = _FakeCustomStatusApi(exc=ApiException(status=403))
        with pytest.raises(ApiException):
            get_workflow_job_status(api, _FakeV1NoConfigMap(), "ns", "wf-abc")

    def test_running_job(self):
        jobset = {"status": {"replicatedJobsStatus": [{"active": 1}]}}
        api = _FakeCustomStatusApi(response=jobset)
        status, dt = get_workflow_job_status(api, _FakeV1NoConfigMap(), "ns", "wf-abc")
        assert status == Status.RUNNING
        assert dt is None

    def test_failed_terminal_state_is_error_regardless_of_phases(self):
        """A JobSet Failed terminalState now only means the controller process
        itself crashed and exhausted backoffLimit — never a deliberate exit."""
        jobset = {"status": {"terminalState": "Failed"}}
        api = _FakeCustomStatusApi(response=jobset)
        status, _ = get_workflow_job_status(api, _FakeV1NoConfigMap(), "ns", "wf-abc")
        assert status == Status.ERROR

    def test_completed_job_reads_phases_configmap_to_detect_cancellation(self):
        jobset = {"status": {"terminalState": "Completed"}}
        api = _FakeCustomStatusApi(response=jobset)
        cm = SimpleNamespace(data={"phases": '{"step-a": "CANCELED"}'})
        status, _ = get_workflow_job_status(api, _FakeV1WithConfigMap(cm), "ns", "wf-abc")
        assert status == Status.CANCELED

    def test_completed_job_reads_phases_configmap_to_detect_failure(self):
        jobset = {"status": {"terminalState": "Completed"}}
        api = _FakeCustomStatusApi(response=jobset)
        cm = SimpleNamespace(data={"phases": '{"step-a": "FAILED"}'})
        status, _ = get_workflow_job_status(api, _FakeV1WithConfigMap(cm), "ns", "wf-abc")
        assert status == Status.FAILED

    def test_completed_job_without_configmap_is_succeeded(self):
        jobset = {"status": {"terminalState": "Completed"}}
        api = _FakeCustomStatusApi(response=jobset)
        status, _ = get_workflow_job_status(api, _FakeV1NoConfigMap(), "ns", "wf-abc")
        assert status == Status.SUCCEEDED


# ---------------------------------------------------------------------------
# controller_jobset_status_and_completion
# ---------------------------------------------------------------------------


class TestControllerJobsetStatusAndCompletion:
    def test_failed_terminal_state_is_error(self):
        jobset = {"status": {"terminalState": "Failed"}}
        status, _ = controller_jobset_status_and_completion(jobset)
        assert status == Status.ERROR

    def test_failed_terminal_state_is_error_even_with_cancelled_phase(self):
        jobset = {"status": {"terminalState": "Failed"}}
        cm = SimpleNamespace(data={"phases": '{"step-a": "CANCELED"}'})
        status, _ = controller_jobset_status_and_completion(jobset, cm)
        assert status == Status.ERROR

    def test_completed_without_configmap_is_succeeded(self):
        jobset = {"status": {"terminalState": "Completed"}}
        status, _ = controller_jobset_status_and_completion(jobset)
        assert status == Status.SUCCEEDED

    def test_completed_with_cancelled_phase_is_terminated(self):
        jobset = {"status": {"terminalState": "Completed"}}
        cm = SimpleNamespace(data={"phases": '{"step-a": "CANCELED"}'})
        status, _ = controller_jobset_status_and_completion(jobset, cm)
        assert status == Status.CANCELED

    def test_completed_with_failed_phase_is_failed(self):
        jobset = {"status": {"terminalState": "Completed"}}
        cm = SimpleNamespace(data={"phases": '{"step-a": "FAILED"}'})
        status, _ = controller_jobset_status_and_completion(jobset, cm)
        assert status == Status.FAILED

    def test_completed_prefers_canceled_over_failed(self):
        """A user-initiated cancellation is checked before a failure, so a
        workflow with both a FAILED and a CANCELED step reports CANCELED
        — the cancellation was the deciding action, regardless of what else
        happened. This is controller_jobset_status_and_completion()'s own
        precedence (workflow_cancelled() before workflow_failed()), not
        aggregate()'s general child-status priority."""
        jobset = {"status": {"terminalState": "Completed"}}
        cm = SimpleNamespace(data={"phases": '{"step-a": "FAILED", "step-b": "CANCELED"}'})
        status, _ = controller_jobset_status_and_completion(jobset, cm)
        assert status == Status.CANCELED


# ---------------------------------------------------------------------------
# read_phases_configmap / workflow_cancelled
# ---------------------------------------------------------------------------


class _FakeV1WithConfigMap:
    def __init__(self, configmap):
        self._configmap = configmap

    def read_namespaced_config_map(self, **kwargs):
        return self._configmap


class TestReadPhasesConfigmap:
    def test_returns_none_on_404(self):
        assert read_phases_configmap(_FakeV1NoConfigMap(), "ns", "wf-abc") is None

    def test_non_404_api_exception_raises(self):
        class _FakeV1Broken:
            def read_namespaced_config_map(self, **kwargs):
                raise ApiException(status=500)

        with pytest.raises(ApiException):
            read_phases_configmap(_FakeV1Broken(), "ns", "wf-abc")

    def test_returns_configmap_when_present(self):
        cm = SimpleNamespace(data={"phases": "{}"})
        assert read_phases_configmap(_FakeV1WithConfigMap(cm), "ns", "wf-abc") is cm


class TestWorkflowCancelled:
    def test_none_configmap_is_not_cancelled(self):
        assert workflow_cancelled(None) is False

    def test_missing_phases_key_is_not_cancelled(self):
        assert workflow_cancelled(SimpleNamespace(data={})) is False

    def test_no_cancelled_step_is_not_cancelled(self):
        cm = SimpleNamespace(data={"phases": '{"step-a": "FAILED", "step-b": "SUCCEEDED"}'})
        assert workflow_cancelled(cm) is False

    def test_any_cancelled_step_is_cancelled(self):
        cm = SimpleNamespace(data={"phases": '{"step-a": "SUCCEEDED", "step-b": "CANCELED"}'})
        assert workflow_cancelled(cm) is True


class TestWorkflowFailed:
    def test_none_configmap_is_not_failed(self):
        assert workflow_failed(None) is False

    def test_missing_phases_key_is_not_failed(self):
        assert workflow_failed(SimpleNamespace(data={})) is False

    def test_no_failed_step_is_not_failed(self):
        cm = SimpleNamespace(data={"phases": '{"step-a": "SUCCEEDED", "step-b": "CANCELED"}'})
        assert workflow_failed(cm) is False

    def test_any_failed_step_is_failed(self):
        cm = SimpleNamespace(data={"phases": '{"step-a": "SUCCEEDED", "step-b": "FAILED"}'})
        assert workflow_failed(cm) is True

    def test_optional_failed_step_is_not_failed(self):
        cm = SimpleNamespace(
            data={
                "phases": '{"step-a": "SUCCEEDED", "step-b": "FAILED"}',
                "optional_steps": '["step-b"]',
            }
        )
        assert workflow_failed(cm) is False

    def test_non_optional_failed_step_is_still_failed_alongside_optional_steps(self):
        cm = SimpleNamespace(
            data={
                "phases": '{"step-a": "FAILED", "step-b": "FAILED"}',
                "optional_steps": '["step-b"]',
            }
        )
        assert workflow_failed(cm) is True


class TestDetail:
    def test_all_values_present(self):
        values = {d.value for d in Detail}
        assert values == {
            "INIT:WAITING",
            "INIT:RUNNING",
            "INIT:ERROR",
            "PULL:ERROR",
            "PULL:CLOSURE",
            "PULLING",
        }
