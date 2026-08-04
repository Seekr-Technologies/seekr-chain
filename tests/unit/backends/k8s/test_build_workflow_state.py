"""
Unit tests for build_workflow_state() — the pure controller-JobSet/worker-JobSet/Pod
-> WorkflowState tree-builder shared by get_workflow_state() and workflow_state_watcher().

Uses types.SimpleNamespace to build minimal fake K8s pod objects, mirroring
test_collect_states.py's fixture style. The controller JobSet is dict-shaped,
matching the real CustomObjectsApi response shape.
"""

from dataclasses import asdict
from types import SimpleNamespace

from seekr_chain.backends.k8s.workflow_state import (
    build_workflow_state,
    controller_jobset_status_and_completion,
)
from seekr_chain.status import PodStatus, WorkflowStatus


def _controller_jobset(active=0, labels=None, annotations=None):
    replicated_status = [{"active": active}] if active else []
    return {
        "metadata": {"labels": labels or {}, "annotations": annotations or {}},
        "status": {"replicatedJobsStatus": replicated_status},
    }


def _jobset(name, step_name, suspend=False):
    return {
        "metadata": {"name": name, "labels": {"seekr-chain/step-name": step_name}},
        "spec": {"suspend": suspend},
        "status": {},
    }


def _pod(name, step, role=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels={
                "seekr-chain/step": step,
                "seekr-chain/role": role,
                "jobset.sigs.k8s.io/job-index": "0",
                "jobset.sigs.k8s.io/job-global-index": "0",
                "jobset.sigs.k8s.io/restart-attempt": "0",
            },
        ),
        status=SimpleNamespace(
            phase="Pending",
            start_time=None,
            init_container_statuses=None,
            container_statuses=None,
        ),
    )


def _state_dict(state):
    """asdict() minus captured_at, which is a live timestamp not worth pinning in these tests."""
    d = asdict(state)
    d.pop("captured_at")
    return d


def test_build_workflow_state_with_no_job_is_unknown():
    state = build_workflow_state("wf-1", controller_jobset=None, jobsets=[], pods=[])
    assert _state_dict(state) == {
        "id": "wf-1",
        "name": None,
        "status": WorkflowStatus.UNKNOWN,
        "dt_start": None,
        "dt_end": None,
        "total_steps": None,
        "steps": [],
    }


def test_build_workflow_state_reads_job_metadata():
    controller_jobset = _controller_jobset(
        active=1,
        labels={"seekr-chain/job-name": "my-job"},
        annotations={"seekr-chain/step-count": "2"},
    )
    state = build_workflow_state("wf-1", controller_jobset=controller_jobset, jobsets=[], pods=[])
    assert _state_dict(state) == {
        "id": "wf-1",
        "name": "my-job",
        "status": WorkflowStatus.RUNNING,
        "dt_start": None,
        "dt_end": None,
        "total_steps": 2,
        "steps": [],
    }


def test_build_workflow_state_groups_jobsets_and_pods_by_step():
    controller_jobset = _controller_jobset(active=1)
    jobsets = [_jobset("wf-1-step-a", "step-a"), _jobset("wf-1-step-b", "step-b")]
    pods = [
        _pod("wf-1-step-a-0", "step-a", role="worker"),
        _pod("wf-1-step-a-1", "step-a", role="worker"),
        _pod("wf-1-step-b-0", "step-b", role=None),
    ]
    state = build_workflow_state("wf-1", controller_jobset=controller_jobset, jobsets=jobsets, pods=pods)

    steps_by_name = {s.name: s for s in state.steps}
    assert set(steps_by_name) == {"step-a", "step-b"}

    step_a = steps_by_name["step-a"]
    assert len(step_a.roles) == 1
    assert step_a.roles[0].name == "worker"
    assert {p.name for p in step_a.roles[0].pods} == {"wf-1-step-a-0", "wf-1-step-a-1"}

    step_b = steps_by_name["step-b"]
    assert len(step_b.roles) == 1
    assert [p.name for p in step_b.roles[0].pods] == ["wf-1-step-b-0"]


def test_build_workflow_state_step_with_no_pods_still_appears():
    controller_jobset = _controller_jobset(active=0)
    jobsets = [_jobset("wf-1-step-a", "step-a", suspend=True)]
    state = build_workflow_state("wf-1", controller_jobset=controller_jobset, jobsets=jobsets, pods=[])
    assert len(state.steps) == 1
    assert state.steps[0].name == "step-a"
    assert state.steps[0].roles == []
    assert state.steps[0].pod.status == PodStatus.PENDING


class TestControllerJobsetTerminalStateMapping:
    def _jobset(self, terminal_state, annotations=None):
        return {
            "metadata": {"annotations": annotations or {}},
            "status": {"terminalState": terminal_state} if terminal_state else {"replicatedJobsStatus": []},
        }

    def test_completed_without_annotation_is_succeeded(self):
        status, _ = controller_jobset_status_and_completion(self._jobset("Completed"))
        assert status == WorkflowStatus.SUCCEEDED

    def test_completed_with_cancelled_annotation_is_terminated(self):
        jobset = self._jobset("Completed", annotations={"seekr-chain/terminal-state": "CANCELLED"})
        status, _ = controller_jobset_status_and_completion(jobset)
        assert status == WorkflowStatus.TERMINATED

    def test_completed_with_other_annotation_value_is_succeeded(self):
        jobset = self._jobset("Completed", annotations={"seekr-chain/terminal-state": "SOMETHING_ELSE"})
        status, _ = controller_jobset_status_and_completion(jobset)
        assert status == WorkflowStatus.SUCCEEDED

    def test_failed_ignores_cancelled_annotation(self):
        jobset = self._jobset("Failed", annotations={"seekr-chain/terminal-state": "CANCELLED"})
        status, _ = controller_jobset_status_and_completion(jobset)
        assert status == WorkflowStatus.FAILED

    def test_no_terminal_state_no_active_is_pending(self):
        status, _ = controller_jobset_status_and_completion(self._jobset(None))
        assert status == WorkflowStatus.PENDING
