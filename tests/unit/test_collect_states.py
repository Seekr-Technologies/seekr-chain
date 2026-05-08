"""
Unit tests for _collect_container_states(), _collect_pod_state(), and _is_jobset_suspended().

Uses types.SimpleNamespace to build minimal fake K8s objects.
"""

from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from seekr_chain.backends.k8s.k8s_workflow import (
    _collect_container_states,
    _collect_pod_state,
    _is_jobset_suspended,
    _trim_pull_message,
)
from seekr_chain.status import ContainerStatus, PodStatus

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


def _pod(phase="Running", init_containers=None, containers=None, labels=None):
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
            start_time=None,
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

    def test_unknown_state_raises(self):
        bad = SimpleNamespace(name="c", state=SimpleNamespace(waiting=None, terminated=None, running=None))
        with pytest.raises(NotImplementedError):
            _collect_container_states([bad], is_init=False)


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
# _is_jobset_suspended
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
        assert _is_jobset_suspended(api, "my-jobset", "argo-workflows") is True

    def test_suspended_false(self):
        api = _FakeCustomApi({"spec": {"suspend": False}})
        assert _is_jobset_suspended(api, "my-jobset", "argo-workflows") is False

    def test_suspend_field_absent(self):
        api = _FakeCustomApi({"spec": {}})
        assert _is_jobset_suspended(api, "my-jobset", "argo-workflows") is False

    def test_api_exception_returns_false(self):
        api = _FakeCustomApi(exc=ApiException(status=404))
        assert _is_jobset_suspended(api, "missing-jobset", "argo-workflows") is False

    def test_unexpected_exception_returns_false(self):
        api = _FakeCustomApi(exc=RuntimeError("unexpected"))
        assert _is_jobset_suspended(api, "my-jobset", "argo-workflows") is False
