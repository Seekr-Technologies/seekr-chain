"""Unit tests for the controller DAG executor (resources/controller.py).

controller.py runs inside the controller pod and has no seekr_chain dependency,
so we import it directly via importlib to avoid any packaging side effects.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: import controller.py as a standalone module without installing it
# ---------------------------------------------------------------------------

_CONTROLLER_PATH = Path(__file__).parent.parent.parent / "src/seekr_chain/backends/k8s/resources/controller.py"


def _load_controller():
    spec = importlib.util.spec_from_file_location("controller", _CONTROLLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


controller = _load_controller()

_cascade_fail = controller._cascade_fail
_submit_ready_steps = controller._submit_ready_steps
_load_manifest = controller._load_manifest
_load_phases = controller._load_phases
_save_phases = controller._save_phases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(js_name: str, terminal: str | None, rv: str = "1", event_type: str = "MODIFIED") -> dict:
    return {
        "type": event_type,
        "object": {
            "metadata": {"name": js_name, "resourceVersion": rv},
            "status": {"terminalState": terminal} if terminal else {},
        },
    }


def _make_k8s_custom(events: list[dict], existing_jobsets: list[str] | None = None):
    """Return a mock CustomObjectsApi that streams the given events."""
    mock = MagicMock()

    # list_namespaced_custom_object returns an object with metadata.resourceVersion;
    # the watch library calls it once to get the initial resourceVersion then streams.
    mock.list_namespaced_custom_object.return_value = {
        "metadata": {"resourceVersion": "0"},
        "items": [],
    }

    if existing_jobsets:
        from kubernetes.client.exceptions import ApiException

        def _create_side_effect(*args, **kwargs):
            body = kwargs.get("body", {})
            name = body.get("metadata", {}).get("name", "")
            if name in existing_jobsets:
                raise ApiException(status=409)

        mock.create_namespaced_custom_object.side_effect = _create_side_effect

    return mock


# ---------------------------------------------------------------------------
# _cascade_fail
# ---------------------------------------------------------------------------


class TestCascadeFail:
    def test_no_failures_no_change(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "PENDING"}
        _cascade_fail(dag, phases)
        assert phases["b"] == "PENDING"

    def test_direct_dep_failed(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "PENDING"}
        _cascade_fail(dag, phases)
        assert phases["b"] == "FAILED"

    def test_transitive_cascade(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        phases = {"a": "FAILED", "b": "PENDING", "c": "PENDING"}
        _cascade_fail(dag, phases)
        assert phases["b"] == "FAILED"
        assert phases["c"] == "FAILED"

    def test_diamond_only_one_branch_fails(self):
        """a→b, a→c, b+c→d; only b fails — d should cascade-fail."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "FAILED", "c": "SUCCEEDED", "d": "PENDING"}
        _cascade_fail(dag, phases)
        assert phases["d"] == "FAILED"

    def test_running_step_not_cascade_failed(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "FAILED", "b": "RUNNING"}
        _cascade_fail(dag, phases)
        # RUNNING steps are not touched — they were already submitted
        assert phases["b"] == "RUNNING"


# ---------------------------------------------------------------------------
# _submit_ready_steps
# ---------------------------------------------------------------------------


class TestSubmitReadySteps:
    def _call(self, dag, phases, existing_jobsets=None):
        js_names: dict = {}
        js_to_step: dict = {}
        mock_k8s = _make_k8s_custom([], existing_jobsets=existing_jobsets)

        with patch.object(controller, "_load_manifest") as mock_load:
            mock_load.side_effect = lambda _assets, name: {
                "metadata": {"name": f"{name}-js"},
                "spec": {},
            }
            _submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)

        return js_names, js_to_step, mock_k8s

    def test_no_dep_step_submitted(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names, js_to_step, mock_k8s = self._call(dag, phases)
        assert phases["a"] == "RUNNING"
        assert js_names["a"] == "a-js"
        assert js_to_step["a-js"] == "a"
        mock_k8s.create_namespaced_custom_object.assert_called_once()

    def test_blocked_step_not_submitted(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "PENDING", "b": "PENDING"}
        js_names, js_to_step, mock_k8s = self._call(dag, phases)
        assert phases["a"] == "RUNNING"
        assert phases["b"] == "PENDING"
        assert mock_k8s.create_namespaced_custom_object.call_count == 1

    def test_unblocked_after_dep_succeeds(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        phases = {"a": "SUCCEEDED", "b": "PENDING"}
        js_names, js_to_step, mock_k8s = self._call(dag, phases)
        assert phases["b"] == "RUNNING"

    def test_409_conflict_treated_as_already_running(self):
        """On controller restart, a JobSet may already exist — 409 should not raise."""
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names, js_to_step, mock_k8s = self._call(dag, phases, existing_jobsets=["a-js"])
        assert phases["a"] == "RUNNING"
        assert js_names["a"] == "a-js"

    def test_non_409_api_error_raises(self):
        from kubernetes.client.exceptions import ApiException

        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names: dict = {}
        js_to_step: dict = {}
        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.side_effect = ApiException(status=500)

        with patch.object(controller, "_load_manifest") as mock_load:
            mock_load.return_value = {"metadata": {"name": "a-js"}, "spec": {}}
            with pytest.raises(ApiException):
                _submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)


# ---------------------------------------------------------------------------
# _load_phases / _save_phases
# ---------------------------------------------------------------------------


class TestLoadPhases:
    def _make_v1(self, cm_data: dict | None = None, status: int | None = None):
        """Return a mock CoreV1Api for ConfigMap reads."""
        from kubernetes.client.exceptions import ApiException

        mock = MagicMock()
        if status is not None:
            mock.read_namespaced_config_map.side_effect = ApiException(status=status)
        elif cm_data is not None:
            import json

            cm = MagicMock()
            cm.data = {"phases": json.dumps(cm_data)}
            mock.read_namespaced_config_map.return_value = cm
        else:
            mock.read_namespaced_config_map.side_effect = ApiException(status=404)
        return mock

    def test_no_configmap_returns_all_pending(self):
        dag = [{"name": "a"}, {"name": "b"}]
        phases = _load_phases(self._make_v1(status=404), "ns", "wf-abc", dag)
        assert phases == {"a": "PENDING", "b": "PENDING"}

    def test_restores_succeeded_and_failed(self):
        dag = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        saved = {"a": "SUCCEEDED", "b": "FAILED", "c": "RUNNING"}
        phases = _load_phases(self._make_v1(cm_data=saved), "ns", "wf-abc", dag)
        assert phases["a"] == "SUCCEEDED"
        assert phases["b"] == "FAILED"
        # RUNNING is reset to PENDING on restore
        assert phases["c"] == "PENDING"

    def test_ignores_unknown_step_names(self):
        """ConfigMap may contain stale step names that no longer exist in the DAG."""
        dag = [{"name": "a"}]
        saved = {"a": "SUCCEEDED", "stale-step": "FAILED"}
        phases = _load_phases(self._make_v1(cm_data=saved), "ns", "wf-abc", dag)
        assert phases == {"a": "SUCCEEDED"}
        assert "stale-step" not in phases

    def test_non_404_api_error_is_warned_not_raised(self):
        dag = [{"name": "a"}]
        # 500 error should not propagate — fall back to all-PENDING
        phases = _load_phases(self._make_v1(status=500), "ns", "wf-abc", dag)
        assert phases == {"a": "PENDING"}


class TestSavePhases:
    def test_creates_configmap_when_not_exists(self):
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        # patch() fails with 404 → create() is called
        mock_v1.patch_namespaced_config_map.side_effect = ApiException(status=404)
        mock_v1.create_namespaced_config_map.return_value = {}

        _save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, [])

        mock_v1.create_namespaced_config_map.assert_called_once()

    def test_patches_existing_configmap(self):
        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.return_value = {}

        _save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, [])

        mock_v1.patch_namespaced_config_map.assert_called_once()
        mock_v1.create_namespaced_config_map.assert_not_called()

    def test_api_error_does_not_raise(self):
        """_save_phases must be best-effort — errors are logged, not raised."""
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.side_effect = ApiException(status=500)

        # Should not raise
        _save_phases(mock_v1, "ns", "wf-abc", {"a": "SUCCEEDED"}, [])


# ---------------------------------------------------------------------------
# main() — end-to-end DAG execution via mocked watch stream
# ---------------------------------------------------------------------------


def _run_main(
    dag_json: list[dict],
    event_sequences: list[list[dict]],
    existing_jobsets: list[str] | None = None,
    initial_phases: dict[str, str] | None = None,
):
    """Run controller.main() with a mocked environment and watch stream.

    event_sequences: list of event batches, one per watch stream open() call.
    Each batch is exhausted before the next watch reconnect (if any).
    """
    env = {
        "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
        "SEEKR_CHAIN_NAMESPACE": "ns",
        "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
        "SEEKR_CHAIN_CONTROLLER_JOB_UID": "uid-123",
    }

    call_count = [0]

    def _stream_side_effect(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(event_sequences):
            yield from event_sequences[idx]

    mock_watch_cls = MagicMock()
    mock_watch_instance = MagicMock()
    mock_watch_instance.stream.side_effect = _stream_side_effect
    mock_watch_instance.stop = MagicMock()
    mock_watch_cls.return_value = mock_watch_instance

    mock_k8s = MagicMock()
    mock_k8s.create_namespaced_custom_object.return_value = {}
    if existing_jobsets:
        from kubernetes.client.exceptions import ApiException

        def _create_side_effect(*args, **kwargs):
            body = kwargs.get("body", {})
            name = body.get("metadata", {}).get("name", "")
            if name in existing_jobsets:
                raise ApiException(status=409)

        mock_k8s.create_namespaced_custom_object.side_effect = _create_side_effect

    mock_custom_api_cls = MagicMock(return_value=mock_k8s)
    mock_core_v1 = MagicMock()
    mock_core_v1_cls = MagicMock(return_value=mock_core_v1)

    def _load_manifest_mock(_assets, name):
        return {"metadata": {"name": f"{name}-js", "resourceVersion": "1"}, "spec": {}}

    # _load_phases: return persisted state if provided, otherwise all-PENDING
    def _load_phases_mock(_v1, _ns, _wid, dag):
        if initial_phases is not None:
            return dict(initial_phases)
        return {s["name"]: "PENDING" for s in dag}

    with (
        patch.dict("os.environ", env),
        patch.object(controller.kubernetes.config, "load_incluster_config"),
        patch.object(controller.kubernetes.client, "CustomObjectsApi", mock_custom_api_cls),
        patch.object(controller.kubernetes.client, "CoreV1Api", mock_core_v1_cls),
        patch.object(controller.kubernetes, "watch", MagicMock(Watch=mock_watch_cls)),
        patch.object(controller, "_load_manifest", side_effect=_load_manifest_mock),
        patch.object(controller, "_load_phases", side_effect=_load_phases_mock),
        patch.object(controller, "_save_phases"),
        patch.object(controller, "_emit_event"),
        patch.object(controller, "_touch_heartbeat"),
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
        patch.object(controller.json, "load", return_value=dag_json),
    ):
        result = controller.main()

    return result


class TestMainLinearDag:
    def test_single_step_success(self):
        dag = [{"name": "a", "depends_on": []}]
        events = [
            [_make_event("a-js", "Completed", rv="2")],
        ]
        assert _run_main(dag, events) == 0

    def test_single_step_failure(self):
        dag = [{"name": "a", "depends_on": []}]
        events = [
            [_make_event("a-js", "Failed", rv="2")],
        ]
        assert _run_main(dag, events) == 1

    def test_linear_two_steps(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        # a completes, then b completes
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Completed", rv="3"),
            ],
        ]
        assert _run_main(dag, events) == 0

    def test_linear_step_b_fails_returns_1(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Failed", rv="3"),
            ],
        ]
        assert _run_main(dag, events) == 1

    def test_step_a_failure_cascade_fails_b(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        # Only a-js fires; b should cascade-fail without being submitted
        events = [
            [_make_event("a-js", "Failed", rv="2")],
        ]
        assert _run_main(dag, events) == 1


class TestMainDiamondDag:
    def test_diamond_all_succeed(self):
        """a → b, a → c, b+c → d."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Completed", rv="3"),
                _make_event("c-js", "Completed", rv="4"),
                _make_event("d-js", "Completed", rv="5"),
            ],
        ]
        assert _run_main(dag, events) == 0

    def test_diamond_b_fails_d_cascade_fails(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Failed", rv="3"),
                _make_event("c-js", "Completed", rv="4"),
            ],
        ]
        assert _run_main(dag, events) == 1


class TestMainWatchReconnect:
    def test_reconnects_after_generic_exception(self):
        """Watch stream raises an exception; controller reconnects and completes."""
        # First stream raises; second stream delivers the completion event.
        call_count = [0]

        def _stream_side_effect(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                raise Exception("transient network error")
            yield _make_event("a-js", "Completed", rv="2")

        mock_watch_cls = MagicMock()
        mock_watch_instance = MagicMock()
        mock_watch_instance.stream.side_effect = _stream_side_effect
        mock_watch_cls.return_value = mock_watch_instance

        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
            "SEEKR_CHAIN_CONTROLLER_JOB_UID": "uid-123",
        }

        dag = [{"name": "a", "depends_on": []}]

        with (
            patch.dict("os.environ", env),
            patch.object(controller.kubernetes.config, "load_incluster_config"),
            patch.object(controller.kubernetes.client, "CustomObjectsApi", MagicMock(return_value=mock_k8s)),
            patch.object(controller.kubernetes.client, "CoreV1Api", MagicMock()),
            patch.object(controller.kubernetes, "watch", MagicMock(Watch=mock_watch_cls)),
            patch.object(controller, "_load_manifest", return_value={"metadata": {"name": "a-js"}, "spec": {}}),
            patch.object(
                controller, "_load_phases", side_effect=lambda _v1, _ns, _wid, d: {s["name"]: "PENDING" for s in d}
            ),
            patch.object(controller, "_save_phases"),
            patch.object(controller, "_emit_event"),
            patch.object(controller, "_touch_heartbeat"),
            patch.object(controller.json, "load", return_value=dag),
            patch.object(controller.time, "sleep"),
            patch("builtins.open", MagicMock(__enter__=lambda s, *a: s, __exit__=lambda s, *a: None)),
        ):
            result = controller.main()

        assert result == 0
        assert call_count[0] == 2  # streamed twice: once failed, once succeeded

    def test_reconnects_after_410_gone(self):
        """410 Gone resets resourceVersion and reconnects."""
        from kubernetes.client.exceptions import ApiException

        call_count = [0]
        rv_used = []

        def _stream_side_effect(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            rv_used.append(kwargs.get("resource_version", ""))
            if idx == 0:
                raise ApiException(status=410)
            yield _make_event("a-js", "Completed", rv="5")

        mock_watch_cls = MagicMock()
        mock_watch_instance = MagicMock()
        mock_watch_instance.stream.side_effect = _stream_side_effect
        mock_watch_cls.return_value = mock_watch_instance

        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
            "SEEKR_CHAIN_CONTROLLER_JOB_UID": "uid-123",
        }

        dag = [{"name": "a", "depends_on": []}]

        with (
            patch.dict("os.environ", env),
            patch.object(controller.kubernetes.config, "load_incluster_config"),
            patch.object(controller.kubernetes.client, "CustomObjectsApi", MagicMock(return_value=mock_k8s)),
            patch.object(controller.kubernetes.client, "CoreV1Api", MagicMock()),
            patch.object(controller.kubernetes, "watch", MagicMock(Watch=mock_watch_cls)),
            patch.object(controller, "_load_manifest", return_value={"metadata": {"name": "a-js"}, "spec": {}}),
            patch.object(
                controller, "_load_phases", side_effect=lambda _v1, _ns, _wid, d: {s["name"]: "PENDING" for s in d}
            ),
            patch.object(controller, "_save_phases"),
            patch.object(controller, "_emit_event"),
            patch.object(controller, "_touch_heartbeat"),
            patch.object(controller.json, "load", return_value=dag),
            patch.object(controller.time, "sleep"),
            patch("builtins.open", MagicMock(__enter__=lambda s, *a: s, __exit__=lambda s, *a: None)),
        ):
            result = controller.main()

        assert result == 0
        # After 410, resourceVersion should be reset to "" for the retry
        assert rv_used[1] == ""


class TestMainControllerRetry:
    def test_409_on_submit_treated_as_resume(self):
        """Controller pod restarted: JobSet already exists (409). Should resume, not crash."""
        dag = [{"name": "a", "depends_on": []}]
        events = [
            [_make_event("a-js", "Completed", rv="2")],
        ]
        result = _run_main(dag, events, existing_jobsets=["a-js"])
        assert result == 0

    def test_multi_step_partial_resume(self):
        """Controller restarts after step a was already submitted but not yet complete.
        Step b has not been submitted yet."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        # a-js already exists; controller resumes watching and completes normally
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Completed", rv="3"),
            ],
        ]
        result = _run_main(dag, events, existing_jobsets=["a-js"])
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
        # Persisted state: a already done, b still pending
        persisted = {"a": "SUCCEEDED", "b": "PENDING"}
        events = [
            # Only b fires — a's JobSet is gone / already terminal
            [_make_event("b-js", "Completed", rv="3")],
        ]

        mock_custom = MagicMock()
        mock_custom.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
            "SEEKR_CHAIN_CONTROLLER_JOB_UID": "uid-123",
        }

        call_count = [0]

        def _stream_side_effect(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(events):
                yield from events[idx]

        mock_watch_cls = MagicMock()
        mock_watch_instance = MagicMock()
        mock_watch_instance.stream.side_effect = _stream_side_effect
        mock_watch_instance.stop = MagicMock()
        mock_watch_cls.return_value = mock_watch_instance

        def _load_manifest_mock(_assets, name):
            return {"metadata": {"name": f"{name}-js"}, "spec": {}}

        with (
            patch.dict("os.environ", env),
            patch.object(controller.kubernetes.config, "load_incluster_config"),
            patch.object(controller.kubernetes.client, "CustomObjectsApi", MagicMock(return_value=mock_custom)),
            patch.object(controller.kubernetes.client, "CoreV1Api", MagicMock()),
            patch.object(controller.kubernetes, "watch", MagicMock(Watch=mock_watch_cls)),
            patch.object(controller, "_load_manifest", side_effect=_load_manifest_mock),
            patch.object(controller, "_load_phases", return_value=dict(persisted)),
            patch.object(controller, "_save_phases"),
            patch.object(controller, "_emit_event"),
            patch.object(controller, "_touch_heartbeat"),
            patch.object(controller.json, "load", return_value=dag),
            patch("builtins.open", MagicMock(__enter__=lambda s, *a: s, __exit__=lambda s, *a: None)),
        ):
            result = controller.main()

        assert result == 0

        # a's JobSet must never be submitted — it was already done before the restart
        submitted = [
            call.kwargs.get("body", {}).get("metadata", {}).get("name")
            for call in mock_custom.create_namespaced_custom_object.call_args_list
        ]
        assert "a-js" not in submitted
        assert "b-js" in submitted
