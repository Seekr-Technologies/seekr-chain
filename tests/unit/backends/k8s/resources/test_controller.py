"""Unit tests for the controller DAG executor (resources/controller package).

The controller package runs inside the controller pod and has no seekr_chain
dependency, so we put the resources dir on sys.path and import the package
modules directly, avoiding any packaging side effects (e.g. seekr_chain/__init__
pulling in boto3/kubernetes at import time).
"""

import copy
import json
import sys
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make controller importable as a top-level package, exactly as it
# is when the controller pod runs `python -m controller`.
# ---------------------------------------------------------------------------

_RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
sys.path.insert(0, str(_RESOURCES))

from controller import manifests, phases, scheduling, status, watch  # noqa: E402

from .fake_k8s import FakeK8sCluster  # noqa: E402

cascade_fail = phases.cascade_fail
submit_ready_steps = scheduling.submit_ready_steps
load_manifest = manifests.load_manifest
load_phases = phases.load_phases
save_phases = phases.save_phases
_workflow_status_from_phases = status._workflow_status_from_phases
_build_status = status._build_status
write_status = status.write_status
_stamp_starts = watch._stamp_starts
_stamp_ends = watch._stamp_ends


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_manifest_mock(_assets, name):
    """Stand in for controller.manifests.load_manifest. Includes the
    seekr-chain/step-name label real JobSet manifests carry (see
    templates/jobset.yaml.j2) — FakeK8sCluster reads it back to resolve a
    create call to the step that was scripted with script_step()."""
    return {"metadata": {"name": f"{name}-js", "labels": {"seekr-chain/step-name": name}}, "spec": {}}


def _record_status_call(cluster: FakeK8sCluster, action_type: str, workflow_id, dag, phases, timings) -> None:
    """side_effect for watch.write_status/watch.flush_status: status.py's
    file write + S3 ship have no place in an in-memory fake, so record the
    call on cluster.actions instead."""
    cluster.actions.append(
        {
            "type": action_type,
            "workflow_id": workflow_id,
            "phases": dict(phases),
            "timings": copy.deepcopy(timings),
        }
    )


def run_controller_main(
    cluster: FakeK8sCluster,
    dag: list[dict],
    *,
    job_name: str = "wf-abc",
    namespace: str = "ns",
    assets_path: str = "/assets",
):
    """Patch kubernetes.config/client/watch to `cluster`, patch open()/json.load
    for dag.json and write_status/flush_status to record onto cluster.actions,
    set env vars, call controller.watch.main(), return (result, cluster)."""
    if job_name not in cluster.jobsets:
        cluster.set_controller_jobset(job_name, "uid-123")

    env = {
        "SEEKR_CHAIN_JOB_ASSET_PATH": assets_path,
        "SEEKR_CHAIN_NAMESPACE": namespace,
        "SEEKR_CHAIN_CONTROLLER_JOB_NAME": job_name,
    }

    with (
        patch.dict("os.environ", env),
        patch.object(watch.kubernetes.config, "load_incluster_config"),
        patch.object(watch.kubernetes.client, "CustomObjectsApi", cluster.custom_objects_api),
        patch.object(watch.kubernetes.client, "CoreV1Api", cluster.core_v1_api),
        patch.object(watch.kubernetes.watch, "Watch", cluster.watch),
        patch.object(scheduling, "load_manifest", side_effect=_load_manifest_mock),
        patch.object(watch.time, "sleep"),
        patch.object(watch, "write_status", side_effect=partial(_record_status_call, cluster, "write_status")),
        patch.object(watch, "flush_status", side_effect=partial(_record_status_call, cluster, "flush_status")),
        patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=lambda s, *a: s,
                    __exit__=lambda s, *a: None,
                    read=MagicMock(return_value=""),
                )
            ),
        ),
        patch.object(watch.json, "load", return_value=dag),
    ):
        result = watch.main()

    return result, cluster


# ---------------------------------------------------------------------------
# cascade_fail
# ---------------------------------------------------------------------------


class TestCascadeFail:
    def test_no_failures_no_change(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "PENDING"}
        cascade_fail(dag, phases)
        assert phases["b"] == "PENDING"

    def test_direct_dep_failed(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "PENDING"}
        cascade_fail(dag, phases)
        assert phases["b"] == "SKIPPED"

    def test_transitive_cascade(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        phases = {"a": "FAILED", "b": "PENDING", "c": "PENDING"}
        cascade_fail(dag, phases)
        assert phases["b"] == "SKIPPED"
        assert phases["c"] == "SKIPPED"

    def test_diamond_only_one_branch_fails(self):
        """a→b, a→c, b+c→d; only b fails — d should cascade-skip."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "FAILED", "c": "SUCCEEDED", "d": "PENDING"}
        cascade_fail(dag, phases)
        assert phases["d"] == "SKIPPED"

    def test_running_step_not_cascade_failed(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "RUNNING"}
        cascade_fail(dag, phases)
        # RUNNING steps are not touched — they were already submitted
        assert phases["b"] == "RUNNING"

    def test_cancelled_dep_cascades_skipped_not_cancelled(self):
        """A CANCELLED step's dependents never ran either, but they weren't the
        step the user cancelled — they cascade to SKIPPED, not CANCELLED."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        phases = {"a": "CANCELLED", "b": "PENDING", "c": "PENDING"}
        cascade_fail(dag, phases)
        assert phases["a"] == "CANCELLED"
        assert phases["b"] == "SKIPPED"
        assert phases["c"] == "SKIPPED"

    def test_failed_chain_fully_propagates_skipped(self):
        """A(FAILED) → B → C: both B and C end SKIPPED; A stays FAILED."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        phases = {"a": "FAILED", "b": "PENDING", "c": "PENDING"}
        cascade_fail(dag, phases)
        assert phases == {"a": "FAILED", "b": "SKIPPED", "c": "SKIPPED"}


# ---------------------------------------------------------------------------
# submit_ready_steps
# ---------------------------------------------------------------------------


class TestSubmitReadySteps:
    def _call(self, dag, phases, cluster=None):
        cluster = cluster or FakeK8sCluster()
        js_names: dict = {}
        js_to_step: dict = {}

        with patch.object(scheduling, "load_manifest") as mock_load:
            mock_load.side_effect = lambda _assets, name: {
                "metadata": {"name": f"{name}-js"},
                "spec": {},
            }
            submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], cluster.custom_objects_api())

        return js_names, js_to_step, cluster

    def test_no_dep_step_submitted(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names, js_to_step, cluster = self._call(dag, phases)
        assert phases["a"] == "RUNNING"
        assert js_names["a"] == "a-js"
        assert js_to_step["a-js"] == "a"
        assert cluster.create_attempts == 1

    def test_blocked_step_not_submitted(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "PENDING", "b": "PENDING"}
        js_names, js_to_step, cluster = self._call(dag, phases)
        assert phases["a"] == "RUNNING"
        assert phases["b"] == "PENDING"
        assert cluster.create_attempts == 1

    def test_unblocked_after_dep_succeeds(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "PENDING"}
        js_names, js_to_step, cluster = self._call(dag, phases)
        assert phases["b"] == "RUNNING"

    def test_pending_step_with_skipped_dep_not_submitted(self):
        """A step is only submitted once all its deps SUCCEEDED — a SKIPPED
        dep (never ran) must never satisfy that, so the dependent stays
        PENDING for cascade_fail to pick up rather than being submitted."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "SKIPPED", "b": "PENDING"}
        js_names, js_to_step, cluster = self._call(dag, phases)
        assert phases["b"] == "PENDING"
        assert cluster.create_attempts == 0

    def test_409_conflict_treated_as_already_running(self):
        """On controller restart, a JobSet may already exist — 409 should not raise."""
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        cluster = FakeK8sCluster()
        cluster.submit_jobset("a-js")
        js_names, js_to_step, cluster = self._call(dag, phases, cluster=cluster)
        assert phases["a"] == "RUNNING"
        assert js_names["a"] == "a-js"

    def test_retriable_api_error_stays_pending(self):
        """Retriable errors (5xx/429) should be caught, logged, and the step left PENDING."""
        from kubernetes.client.exceptions import ApiException

        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names: dict = {}
        js_to_step: dict = {}
        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.side_effect = ApiException(status=500)

        with patch.object(scheduling, "load_manifest") as mock_load:
            mock_load.return_value = {"metadata": {"name": "a-js"}, "spec": {}}
            submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)

        # Step should remain PENDING — it will be retried on the next iteration
        assert phases["a"] == "PENDING"
        assert js_names == {}
        assert js_to_step == {}

    def test_permanent_api_error_marks_step_failed(self):
        """Permanent errors (4xx) should mark the step FAILED, not retry forever."""
        from kubernetes.client.exceptions import ApiException

        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "PENDING", "b": "PENDING"}
        js_names: dict = {}
        js_to_step: dict = {}
        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.side_effect = ApiException(status=403)

        with patch.object(scheduling, "load_manifest") as mock_load:
            mock_load.return_value = {"metadata": {"name": "a-js"}, "spec": {}}
            submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)

        # Step a should be FAILED (permanent error), not PENDING
        assert phases["a"] == "FAILED"
        assert js_names == {}
        assert js_to_step == {}


# ---------------------------------------------------------------------------
# load_phases / save_phases
# ---------------------------------------------------------------------------


class TestLoadPhases:
    def _make_v1(self, cm_data: dict | None = None, timings_data: dict | None = None, status: int | None = None):
        """Return a mock CoreV1Api for ConfigMap reads."""
        from kubernetes.client.exceptions import ApiException

        mock = MagicMock()
        if status is not None:
            mock.read_namespaced_config_map.side_effect = ApiException(status=status)
        elif cm_data is not None:
            import json

            cm = MagicMock()
            cm.data = {"phases": json.dumps(cm_data)}
            if timings_data is not None:
                cm.data["timings"] = json.dumps(timings_data)
            mock.read_namespaced_config_map.return_value = cm
        else:
            mock.read_namespaced_config_map.side_effect = ApiException(status=404)
        return mock

    def test_no_configmap_returns_all_pending(self):
        dag = [{"name": "a"}, {"name": "b"}]
        phases, timings = load_phases(self._make_v1(status=404), "ns", "wf-abc", dag)
        assert phases == {"a": "PENDING", "b": "PENDING"}
        assert timings == {}

    def test_restores_succeeded_and_failed(self):
        dag = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        saved = {"a": "SUCCEEDED", "b": "FAILED", "c": "RUNNING"}
        phases, _ = load_phases(self._make_v1(cm_data=saved), "ns", "wf-abc", dag)
        assert phases["a"] == "SUCCEEDED"
        assert phases["b"] == "FAILED"
        # RUNNING is reset to PENDING on restore
        assert phases["c"] == "PENDING"

    def test_restores_skipped(self):
        dag = [{"name": "a"}, {"name": "b"}]
        saved = {"a": "FAILED", "b": "SKIPPED"}
        phases, _ = load_phases(self._make_v1(cm_data=saved), "ns", "wf-abc", dag)
        assert phases == {"a": "FAILED", "b": "SKIPPED"}

    def test_ignores_unknown_step_names(self):
        """ConfigMap may contain stale step names that no longer exist in the DAG."""
        dag = [{"name": "a"}]
        saved = {"a": "SUCCEEDED", "stale-step": "FAILED"}
        phases, _ = load_phases(self._make_v1(cm_data=saved), "ns", "wf-abc", dag)
        assert phases == {"a": "SUCCEEDED"}
        assert "stale-step" not in phases

    def test_non_404_api_error_is_warned_not_raised(self):
        dag = [{"name": "a"}]
        # 500 error should not propagate — fall back to all-PENDING
        phases, timings = load_phases(self._make_v1(status=500), "ns", "wf-abc", dag)
        assert phases == {"a": "PENDING"}
        assert timings == {}

    def test_restores_timings_for_terminal_step(self):
        dag = [{"name": "a"}]
        saved = {"a": "SUCCEEDED"}
        saved_timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}
        _, timings = load_phases(self._make_v1(cm_data=saved, timings_data=saved_timings), "ns", "wf-abc", dag)
        assert timings == saved_timings

    def test_drops_timings_for_step_reset_running_to_pending(self):
        """A RUNNING step is reset to PENDING on restore, so its timings must
        be dropped too — otherwise a re-run would carry stale start/end times."""
        dag = [{"name": "a"}]
        saved = {"a": "RUNNING"}
        saved_timings = {"a": {"dt_start": "2026-01-01T00:00:00Z"}}
        phases, timings = load_phases(self._make_v1(cm_data=saved, timings_data=saved_timings), "ns", "wf-abc", dag)
        assert phases == {"a": "PENDING"}
        assert timings == {}


class TestSavePhases:
    def test_creates_configmap_when_not_exists(self):
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        # patch() fails with 404 → create() is called
        mock_v1.patch_namespaced_config_map.side_effect = ApiException(status=404)
        mock_v1.create_namespaced_config_map.return_value = {}

        save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, {}, [])

        mock_v1.create_namespaced_config_map.assert_called_once()

    def test_patches_existing_configmap(self):
        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.return_value = {}

        save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, {}, [])

        mock_v1.patch_namespaced_config_map.assert_called_once()
        mock_v1.create_namespaced_config_map.assert_not_called()

    def test_api_error_does_not_raise(self):
        """save_phases must be best-effort — errors are logged, not raised."""
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.side_effect = ApiException(status=500)

        # Should not raise
        save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, {}, [])

    def test_persists_both_phases_and_timings_keys(self):
        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.return_value = {}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}

        save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, timings, [])

        data = mock_v1.patch_namespaced_config_map.call_args.kwargs["body"]["data"]
        assert json.loads(data["phases"]) == {"a": "SUCCEEDED"}
        assert json.loads(data["timings"]) == timings


class TestDeriveStatus:
    @pytest.mark.parametrize(
        "phases, expected",
        [
            ({"a": "CANCELLED", "b": "FAILED"}, "TERMINATED"),
            ({"a": "CANCELLED", "b": "SUCCEEDED"}, "TERMINATED"),
            ({"a": "FAILED", "b": "SUCCEEDED"}, "FAILED"),
            ({"a": "SUCCEEDED", "b": "SKIPPED"}, "SUCCEEDED"),
            ({"a": "SUCCEEDED", "b": "RUNNING"}, "RUNNING"),
            ({"a": "PENDING", "b": "PENDING"}, "RUNNING"),
        ],
    )
    def test_precedence(self, phases, expected):
        assert _workflow_status_from_phases(phases) == expected


class TestBuildStatus:
    def test_schema_shape_with_skipped_step_and_timings(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "SKIPPED", "c": "SKIPPED"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}

        result = _build_status("wf-abc", dag, phases, timings)

        captured_at = result.pop("captured_at")
        assert isinstance(captured_at, str) and captured_at
        assert result == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "FAILED",
            "steps": [
                {
                    "name": "a",
                    "phase": "FAILED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {"name": "b", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
                {"name": "c", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
            ],
        }

    def test_schema_shape_when_succeeded(self):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        phases = {"a": "SUCCEEDED", "b": "SUCCEEDED"}
        timings = {
            "a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"},
            "b": {"dt_start": "2026-01-01T00:00:06Z", "dt_end": "2026-01-01T00:00:10Z"},
        }

        result = _build_status("wf-abc", dag, phases, timings)

        captured_at = result.pop("captured_at")
        assert isinstance(captured_at, str) and captured_at
        assert result == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "SUCCEEDED",
            "steps": [
                {
                    "name": "a",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {
                    "name": "b",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:06Z",
                    "dt_end": "2026-01-01T00:00:10Z",
                },
            ],
        }

    def test_schema_shape_when_cancelled(self):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        phases = {"a": "SUCCEEDED", "b": "CANCELLED"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}

        result = _build_status("wf-abc", dag, phases, timings)

        captured_at = result.pop("captured_at")
        assert isinstance(captured_at, str) and captured_at
        assert result == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "TERMINATED",
            "steps": [
                {
                    "name": "a",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {"name": "b", "phase": "CANCELLED", "dt_start": None, "dt_end": None},
            ],
        }


class TestWriteStatus:
    """Round-trip tests for write_status(): the actual file writer, previously untested."""

    def _write_and_read(self, tmp_path, monkeypatch, workflow_id, dag, phases, timings):
        monkeypatch.setattr(status, "_STATUS_PATH", str(tmp_path / "status.json"))
        monkeypatch.delenv(status._REMOTE_STATUS_ENV, raising=False)
        write_status(workflow_id, dag, phases, timings)
        with open(tmp_path / "status.json") as f:
            doc = json.load(f)
        captured_at = doc.pop("captured_at")
        assert isinstance(captured_at, str) and captured_at
        return doc

    def test_writes_succeeded_doc(self, tmp_path, monkeypatch):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        phases = {"a": "SUCCEEDED", "b": "SUCCEEDED"}
        timings = {
            "a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"},
            "b": {"dt_start": "2026-01-01T00:00:06Z", "dt_end": "2026-01-01T00:00:10Z"},
        }
        doc = self._write_and_read(tmp_path, monkeypatch, "wf-abc", dag, phases, timings)
        assert doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "SUCCEEDED",
            "steps": [
                {
                    "name": "a",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {
                    "name": "b",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:06Z",
                    "dt_end": "2026-01-01T00:00:10Z",
                },
            ],
        }

    def test_writes_failed_and_skipped_doc(self, tmp_path, monkeypatch):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "SKIPPED", "c": "SKIPPED"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}
        doc = self._write_and_read(tmp_path, monkeypatch, "wf-abc", dag, phases, timings)
        assert doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "FAILED",
            "steps": [
                {
                    "name": "a",
                    "phase": "FAILED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {"name": "b", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
                {"name": "c", "phase": "SKIPPED", "dt_start": None, "dt_end": None},
            ],
        }

    def test_writes_cancelled_doc_as_terminated(self, tmp_path, monkeypatch):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        phases = {"a": "SUCCEEDED", "b": "CANCELLED"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}
        doc = self._write_and_read(tmp_path, monkeypatch, "wf-abc", dag, phases, timings)
        assert doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "TERMINATED",
            "steps": [
                {
                    "name": "a",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {"name": "b", "phase": "CANCELLED", "dt_start": None, "dt_end": None},
            ],
        }

    def test_writes_running_doc(self, tmp_path, monkeypatch):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        phases = {"a": "SUCCEEDED", "b": "RUNNING"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z", "dt_end": "2026-01-01T00:00:05Z"}}
        doc = self._write_and_read(tmp_path, monkeypatch, "wf-abc", dag, phases, timings)
        assert doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "RUNNING",
            "steps": [
                {
                    "name": "a",
                    "phase": "SUCCEEDED",
                    "dt_start": "2026-01-01T00:00:00Z",
                    "dt_end": "2026-01-01T00:00:05Z",
                },
                {"name": "b", "phase": "RUNNING", "dt_start": None, "dt_end": None},
            ],
        }


class TestShipOnce:
    """_ship_once() is the s5cmd upload boundary: it must invoke s5cmd exactly
    when shipping is configured, and never raise regardless of subprocess
    outcome."""

    def test_invokes_s5cmd_with_status_path_and_remote_when_configured(self, monkeypatch):
        monkeypatch.setenv(status._REMOTE_STATUS_ENV, "s3://bucket/jobs/wf/abc/status.json")
        captured = []
        monkeypatch.setattr(status.subprocess, "run", lambda argv, **kwargs: captured.append(argv))
        status._ship_once()
        assert captured == [["s5cmd", "cp", status._STATUS_PATH, "s3://bucket/jobs/wf/abc/status.json"]]

    def test_does_not_invoke_s5cmd_when_remote_path_unset(self, monkeypatch):
        monkeypatch.delenv(status._REMOTE_STATUS_ENV, raising=False)
        calls = []
        monkeypatch.setattr(status.subprocess, "run", lambda *a, **k: calls.append((a, k)))
        status._ship_once()
        assert calls == []

    def test_swallows_subprocess_timeout(self, monkeypatch):
        monkeypatch.setenv(status._REMOTE_STATUS_ENV, "s3://bucket/jobs/wf/abc/status.json")

        def _raise(*a, **k):
            raise status.subprocess.TimeoutExpired(cmd="s5cmd", timeout=15)

        monkeypatch.setattr(status.subprocess, "run", _raise)
        assert status._ship_once() is None

    def test_swallows_generic_exception(self, monkeypatch):
        monkeypatch.setenv(status._REMOTE_STATUS_ENV, "s3://bucket/jobs/wf/abc/status.json")

        def _raise(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(status.subprocess, "run", _raise)
        assert status._ship_once() is None


class TestStampTimings:
    """_stamp_starts/_stamp_ends only record run timestamps for steps that
    actually ran — SKIPPED (and any other never-started) steps must carry
    neither dt_start nor dt_end."""

    def test_skipped_step_gets_no_timestamps(self):
        phases = {"a": "FAILED", "b": "SKIPPED"}
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": ["a"]}]
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z"}}

        _stamp_starts(dag, phases, timings)
        _stamp_ends(phases, timings)

        assert set(timings.get("b", {}).keys()) == set()

    def test_running_step_gets_only_dt_start(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "RUNNING"}
        timings = {}

        _stamp_starts(dag, phases, timings)
        _stamp_ends(phases, timings)

        assert set(timings["a"].keys()) == {"dt_start"}
        assert isinstance(timings["a"]["dt_start"], str) and timings["a"]["dt_start"]

    def test_ran_terminal_step_gets_both_timestamps(self):
        dag = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": []}]
        phases = {"a": "SUCCEEDED", "b": "FAILED"}
        timings = {}

        _stamp_starts(dag, phases, timings)
        _stamp_ends(phases, timings)

        for name in ("a", "b"):
            assert set(timings[name].keys()) == {"dt_start", "dt_end"}
            assert isinstance(timings[name]["dt_start"], str) and timings[name]["dt_start"]
            assert isinstance(timings[name]["dt_end"], str) and timings[name]["dt_end"]

    def test_dt_end_never_set_without_a_prior_dt_start(self):
        """Guards the end-before-start bug: a step reaching a terminal phase
        without ever recording a dt_start (e.g. SKIPPED, cascaded straight
        from PENDING) must not get a dt_end either."""
        phases = {"b": "SKIPPED"}
        timings = {}

        _stamp_ends(phases, timings)

        assert set(timings.get("b", {}).keys()) == set()

    def test_stamp_starts_is_idempotent(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "RUNNING"}
        timings = {"a": {"dt_start": "2026-01-01T00:00:00Z"}}

        _stamp_starts(dag, phases, timings)

        assert timings["a"]["dt_start"] == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# main() — end-to-end DAG execution via mocked watch stream
# ---------------------------------------------------------------------------


class TestMainLinearDag:
    def test_single_step_success(self):
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_single_step_failure(self):
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=1)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_linear_two_steps(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0
        # b is only submitted once a has succeeded.
        assert [a["step"] for a in cluster.actions if a["type"] == "create_jobset"] == ["a", "b"]

    def test_linear_step_b_fails_returns_0(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_step_a_failure_cascade_fails_b(self):
        """Only a-js fires; b should cascade-fail without being submitted."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=1)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0
        # b must never be submitted — a's failure cascade-skips it.
        assert [a["step"] for a in cluster.actions if a["type"] == "create_jobset"] == ["a"]


class TestMainDiamondDag:
    def test_diamond_all_succeed(self):
        """a → b, a → c, b+c → d."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)
        cluster.script_step("c", exit_code=0)
        cluster.script_step("d", exit_code=0)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_diamond_b_fails_d_cascade_fails(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0
        # d must never be submitted — it cascade-skips once b fails.
        assert "d" not in [a["step"] for a in cluster.actions if a["type"] == "create_jobset"]


class TestMainCancellation:
    """Cancellation is triggered externally (`chain cancel` suspends the
    JobSet), not by anything the controller's own create call produces — not
    expressible via script_step, so this class keeps manual mutation."""

    def test_single_step_cancelled_exits(self):
        """A JobSet suspended (chain cancel) with no terminalState must not hang."""
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.cancel_jobset("a-js")
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_cascade_cancels_unsubmitted_dependent(self):
        """a is cancelled before b's dependency is satisfied — b must never be
        submitted and must cascade to CANCELLED instead of hanging."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.cancel_jobset("a-js")
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_diamond_partial_cancel_cascades_join_step(self):
        """a → b, a → c, b+c → d. b is cancelled, c succeeds — d must
        cascade-cancel rather than waiting forever for a JobSet that is
        never submitted."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        # All three pre-populated (not script_step): a's and c's completion
        # events must be enqueued *before* the controller starts, same as
        # b's cancellation, so all three sit in the queue together and
        # js_to_step catches up to each mid-drain as 409-resume tracks it.
        # (script_step only enqueues at create time, which — mixed with an
        # immediately-enqueued cancel — can reorder relative to when the
        # controller starts tracking a JobSet and drop the cancel event.)
        cluster = FakeK8sCluster()
        cluster.complete_jobset("a-js")
        cluster.cancel_jobset("b-js")
        cluster.complete_jobset("c-js")
        result, _ = run_controller_main(cluster, dag)
        assert result == 0


class TestMainSkippedStatus:
    def test_workflow_failed_event_excludes_skipped_steps(self):
        """a → b, a → c, b+c → d; only b fails. The WorkflowFailed event's
        failed-steps list must contain only b — d is SKIPPED (never ran), not
        FAILED, so it must not be reported as a failed step."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)

        result, cluster = run_controller_main(cluster, dag)

        assert result == 0
        failed_events = [e for e in cluster.events if e["reason"] == "WorkflowFailed"]
        assert len(failed_events) == 1
        assert failed_events[0]["message"] == "Workflow failed — failed steps: ['b']"


class TestMainWatchReconnect:
    def test_reconnects_after_generic_exception(self):
        """Watch stream raises an exception; controller reconnects and completes."""
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.raise_on_next_stream(Exception("transient network error"))
        cluster.script_step("a", exit_code=0)

        result, _ = run_controller_main(cluster, dag)

        assert result == 0

    def test_reconnects_after_410_gone(self):
        """410 Gone resets resourceVersion and reconnects.

        Note: the original MagicMock-based test also asserted that the
        resourceVersion passed to the reconnecting stream() call was reset to
        "" — FakeWatch only records the *last* stream() call's kwargs
        (cluster.watch_last_kwargs), not the full history, so that specific
        assertion isn't reproducible through the Fake. The behavior it
        guarded (reconnect-after-410 succeeds) is still covered below.
        """
        from kubernetes.client.exceptions import ApiException

        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.raise_on_next_stream(ApiException(status=410))
        cluster.script_step("a", exit_code=0)

        result, _ = run_controller_main(cluster, dag)

        assert result == 0


class TestMainControllerRetry:
    def test_409_on_submit_treated_as_resume(self):
        """Controller pod restarted: JobSet already exists (409). Should resume, not crash.

        submit_jobset + complete_jobset (not script_step) — the JobSet must
        exist *before* the controller's create call, which script_step can't
        express since it only resolves outcomes at create time.
        """
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.submit_jobset("a-js")
        cluster.complete_jobset("a-js")
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_multi_step_partial_resume(self):
        """Controller restarts after step a was already submitted but not yet complete.
        Step b has not been submitted yet."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.submit_jobset("a-js")
        cluster.complete_jobset("a-js")
        cluster.script_step("b", exit_code=0)
        result, _ = run_controller_main(cluster, dag)
        assert result == 0

    def test_configmap_resume_does_not_resubmit_completed_step(self):
        """Controller restarts after step a already SUCCEEDED (persisted in ConfigMap).

        The watch stream only delivers an event for b — there is no second event
        for a because it finished before the restart.  Without ConfigMap state
        the controller would stall waiting for a's terminal event.  With it,
        a is already SUCCEEDED so b is submitted immediately and the workflow
        completes without touching a's JobSet.
        """
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster = FakeK8sCluster()
        cluster.configmaps["wf-abc-phases"] = {
            "data": {
                "phases": json.dumps({"a": "SUCCEEDED", "b": "PENDING"}),
                "timings": json.dumps({}),
            }
        }
        cluster.script_step("b", exit_code=0)

        result, cluster = run_controller_main(cluster, dag)

        assert result == 0
        # a's JobSet must never be submitted — it was already done before the restart
        assert "a-js" not in cluster.jobsets
        assert "b-js" in cluster.jobsets


# ---------------------------------------------------------------------------
# Watch timeout (Fix 4) and transient submit retry (Fix 3)
# ---------------------------------------------------------------------------


class TestWatchTimeout:
    def test_stream_called_with_timeout_seconds(self):
        """w.stream() must be called with timeout_seconds to prevent stale heartbeat."""
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.script_step("a", exit_code=0)

        result, cluster = run_controller_main(cluster, dag)

        assert result == 0
        assert cluster.watch_last_kwargs["timeout_seconds"] == 30


class TestTransientSubmitRetry:
    def test_step_retried_after_transient_error(self):
        """A 500 on submit leaves the step PENDING; on the next watch iteration
        the retry succeeds and the step completes."""
        dag = [{"name": "a", "depends_on": []}]
        cluster = FakeK8sCluster()
        cluster.fail_next_create(500)
        cluster.script_step("a", exit_code=0)

        result, cluster = run_controller_main(cluster, dag)

        assert result == 0
        # Submit was attempted twice: first failed, second succeeded
        assert cluster.create_attempts == 2
