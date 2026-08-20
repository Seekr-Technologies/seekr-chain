"""
Unit tests for the S3 status.json fallback path:

  * ``build_workflow_state_from_status_doc()`` — pure mapper from an archived
    outcome-only status doc to a ``WorkflowState``.
  * ``get_workflow_state()`` — dispatches to the S3 fallback only when the
    controller JobSet is gone (GC'd); otherwise it must take the live path
    unchanged (regression guard, no S3 read at all).

Uses fakes (not mocks) for the K8s custom-objects/core-v1 clients and for
``remote_fs``, per repo testing conventions.
"""

import kubernetes as k8s

from seekr_chain.backends.k8s import workflow_state
from seekr_chain.backends.k8s.workflow_state import build_workflow_state_from_status_doc, get_workflow_state
from seekr_chain.status import PodStatus, WorkflowStatus


def _controller_jobset(active=1):
    return {
        "metadata": {"labels": {}, "annotations": {}},
        "status": {"replicatedJobsStatus": [{"active": active}] if active else []},
    }


class _FakeCustomObjects:
    """Fake CustomObjectsApi: get_namespaced_custom_object raises 404 (or returns
    a jobset), list_namespaced_custom_object returns an empty JobSet list."""

    def __init__(self, controller_jobset):
        self._controller_jobset = controller_jobset

    def get_namespaced_custom_object(self, **kwargs):
        if self._controller_jobset is None:
            raise k8s.client.exceptions.ApiException(status=404)
        return self._controller_jobset

    def list_namespaced_custom_object(self, **kwargs):
        return {"items": [], "metadata": {}}


class _FakeCoreV1:
    """Fake CoreV1Api: list_namespaced_pod returns no pods; read_namespaced_config_map
    would raise if called (the phases ConfigMap is owned by the controller JobSet,
    so it must not be read once that JobSet is gone)."""

    def list_namespaced_pod(self, **kwargs):
        return type("Resp", (), {"items": [], "metadata": type("Meta", (), {"resource_version": ""})()})()

    def read_namespaced_config_map(self, **kwargs):
        raise AssertionError("phases ConfigMap must not be read once the controller JobSet is gone")


_DOC = {
    "schema_version": 1,
    "id": "wf-1",
    "status": "FAILED",
    "steps": [
        {
            "name": "a",
            "phase": "FAILED",
            "dt_start": "2026-01-01T00:00:00+00:00",
            "dt_end": "2026-01-01T00:01:00+00:00",
        },
        {"name": "b", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
        {"name": "c", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
    ],
    "captured_at": "2026-01-01T00:01:00+00:00",
}


def test_build_workflow_state_from_status_doc_maps_failed_and_skipped_steps():
    state = build_workflow_state_from_status_doc("wf-1", _DOC)

    assert state.status == WorkflowStatus.FAILED
    assert state.total_steps == 3
    steps_by_name = {s.name: s for s in state.steps}
    assert steps_by_name["a"].pod.status == PodStatus.FAILED
    assert steps_by_name["b"].pod.status == PodStatus.SKIPPED
    assert steps_by_name["c"].pod.status == PodStatus.SKIPPED
    assert all(s.roles == [] for s in state.steps)
    assert state.dt_start.isoformat() == "2026-01-01T00:00:00+00:00"
    assert state.dt_end.isoformat() == "2026-01-01T00:01:00+00:00"


def test_get_workflow_state_falls_back_to_s3_when_controller_jobset_is_gone(monkeypatch):
    monkeypatch.setattr(workflow_state, "read_status_doc", lambda workflow_id, datastore_root=None: _DOC)
    state = get_workflow_state(_FakeCustomObjects(None), _FakeCoreV1(), "ns", "wf-1")
    assert state.status == WorkflowStatus.FAILED
    assert {s.name for s in state.steps} == {"a", "b", "c"}


def test_get_workflow_state_never_reads_s3_when_controller_jobset_is_present(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("read_status_doc must not be called when the controller JobSet is present")

    monkeypatch.setattr(workflow_state, "read_status_doc", _fail)
    state = get_workflow_state(_FakeCustomObjects(_controller_jobset()), _FakeCoreV1(), "ns", "wf-1")
    assert state.status == WorkflowStatus.RUNNING


def test_get_workflow_state_falls_through_to_unknown_when_s3_doc_is_also_missing(monkeypatch):
    monkeypatch.setattr(workflow_state, "read_status_doc", lambda workflow_id, datastore_root=None: None)
    state = get_workflow_state(_FakeCustomObjects(None), _FakeCoreV1(), "ns", "wf-1")
    assert state.status == WorkflowStatus.UNKNOWN
    assert state.steps == []


def test_read_status_doc_returns_none_when_object_does_not_exist(monkeypatch):
    monkeypatch.setattr(workflow_state.remote_fs, "exists", lambda path: False)
    result = workflow_state.read_status_doc("wf-1", datastore_root="s3://bucket/root")
    assert result is None


def test_read_status_doc_downloads_and_parses_json(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_state.remote_fs, "exists", lambda path: True)

    def _fake_download(src, dst):
        with open(dst, "w") as f:
            f.write('{"status": "SUCCEEDED", "steps": []}')

    monkeypatch.setattr(workflow_state.remote_fs, "download", _fake_download)
    result = workflow_state.read_status_doc("wf-1", datastore_root="s3://bucket/root")
    assert result == {"status": "SUCCEEDED", "steps": []}


def test_read_status_doc_returns_none_on_unresolvable_datastore_root(monkeypatch):
    monkeypatch.setattr(workflow_state, "get_job_info", lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")))
    assert workflow_state.read_status_doc("wf-1", datastore_root=None) is None
