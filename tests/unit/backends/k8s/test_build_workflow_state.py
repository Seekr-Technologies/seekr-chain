"""
Unit tests for build_workflow_state() — the pure Job/JobSet/Pod -> WorkflowState
tree-builder shared by get_workflow_state() and workflow_state_watcher().

Uses types.SimpleNamespace to build minimal fake K8s objects, mirroring
test_collect_states.py's fixture style.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

from seekr_chain.backends.k8s.workflow_state import build_workflow_state
from seekr_chain.status import PodStatus, WorkflowStatus


def _job(
    succeeded=0,
    failed=0,
    active=0,
    start_time=None,
    completion_time=None,
    conditions=None,
    labels=None,
    annotations=None,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels=labels or {},
            annotations=annotations or {},
            creation_timestamp=None,
        ),
        status=SimpleNamespace(
            succeeded=succeeded,
            failed=failed,
            active=active,
            start_time=start_time,
            completion_time=completion_time,
            conditions=conditions,
        ),
    )


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
    state = build_workflow_state("wf-1", job=None, jobsets=[], pods=[])
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
    job = _job(
        active=1,
        labels={"seekr-chain/job-name": "my-job"},
        annotations={"seekr-chain/step-count": "2"},
    )
    state = build_workflow_state("wf-1", job=job, jobsets=[], pods=[])
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
    job = _job(active=1)
    jobsets = [_jobset("wf-1-step-a", "step-a"), _jobset("wf-1-step-b", "step-b")]
    pods = [
        _pod("wf-1-step-a-0", "step-a", role="worker"),
        _pod("wf-1-step-a-1", "step-a", role="worker"),
        _pod("wf-1-step-b-0", "step-b", role=None),
    ]
    state = build_workflow_state("wf-1", job=job, jobsets=jobsets, pods=pods)

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
    job = _job(active=0)
    jobsets = [_jobset("wf-1-step-a", "step-a", suspend=True)]
    state = build_workflow_state("wf-1", job=job, jobsets=jobsets, pods=[])
    assert len(state.steps) == 1
    assert state.steps[0].name == "step-a"
    assert state.steps[0].roles == []
    assert state.steps[0].pod.status == PodStatus.PENDING


def test_build_workflow_state_failed_job_uses_condition_for_dt_end():
    """A failed Job has no completion_time; dt_end falls back to the Failed condition."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    failed_at = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    job = _job(
        failed=1,
        start_time=start,
        completion_time=None,
        conditions=[SimpleNamespace(type="Failed", status="True", last_transition_time=failed_at)],
    )
    state = build_workflow_state("wf-1", job=job, jobsets=[], pods=[])
    assert state.status == WorkflowStatus.FAILED
    assert state.dt_end == failed_at
