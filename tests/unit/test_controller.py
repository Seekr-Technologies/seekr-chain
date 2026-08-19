"""Unit tests for the controller DAG executor (resources/controller.py).

controller.py runs inside the controller pod and has no seekr_chain dependency,
so we import it directly via importlib to avoid any packaging side effects.
"""

import contextlib
import datetime
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

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
_load_handlers = controller._load_handlers
_read_step_exit_info = controller._read_step_exit_info
_handler_env = controller._handler_env
_inject_handler_env = controller._inject_handler_env
_load_handler_states = controller._load_handler_states
_save_handler_states = controller._save_handler_states
_workflow_settled = controller._workflow_settled
_submit_handlers_for_step = controller._submit_handlers_for_step
_dispatch_handlers_for_terminal_steps = controller._dispatch_handlers_for_terminal_steps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    js_name: str,
    terminal: str | None,
    rv: str = "1",
    event_type: str = "MODIFIED",
    suspend: bool = False,
) -> dict:
    return {
        "type": event_type,
        "object": {
            "metadata": {"name": js_name, "resourceVersion": rv},
            "spec": {"suspend": suspend},
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


def _make_pod(
    name: str,
    role: str = "worker",
    exit_code: int | None = None,
    reason: str = "",
    message: str = "",
    finished_at=None,
    main_terminated: bool = True,
):
    """Build a fake k8s Pod object shaped like the real client's attribute-access model."""
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.labels = {"seekr-chain/role": role}

    main_container = MagicMock()
    main_container.name = "main"
    if main_terminated:
        terminated = MagicMock()
        terminated.exit_code = exit_code
        terminated.reason = reason
        terminated.message = message
        terminated.finished_at = finished_at
        main_container.state.terminated = terminated
    else:
        main_container.state.terminated = None

    pod.status.container_statuses = [main_container]
    return pod


def _make_core_v1_for_pods(pods: list, raises: Exception | None = None):
    mock = MagicMock()
    if raises is not None:
        mock.list_namespaced_pod.side_effect = raises
    else:
        resp = MagicMock()
        resp.items = pods
        mock.list_namespaced_pod.return_value = resp
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

    def test_cancelled_dep_cascades_cancelled_not_failed(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        phases = {"a": "CANCELLED", "b": "PENDING", "c": "PENDING"}
        _cascade_fail(dag, phases)
        assert phases["b"] == "CANCELLED"
        assert phases["c"] == "CANCELLED"


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

    def test_retriable_api_error_stays_pending(self):
        """Retriable errors (5xx/429) should be caught, logged, and the step left PENDING."""
        from kubernetes.client.exceptions import ApiException

        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        js_names: dict = {}
        js_to_step: dict = {}
        mock_k8s = MagicMock()
        mock_k8s.create_namespaced_custom_object.side_effect = ApiException(status=500)

        with patch.object(controller, "_load_manifest") as mock_load:
            mock_load.return_value = {"metadata": {"name": "a-js"}, "spec": {}}
            _submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)

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

        with patch.object(controller, "_load_manifest") as mock_load:
            mock_load.return_value = {"metadata": {"name": "a-js"}, "spec": {}}
            _submit_ready_steps(dag, phases, js_names, js_to_step, "/assets", "ns", [], mock_k8s)

        # Step a should be FAILED (permanent error), not PENDING
        assert phases["a"] == "FAILED"
        assert js_names == {}
        assert js_to_step == {}


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
    handlers: dict[str, list[dict]] | None = None,
    initial_handler_states: dict[str, str] | None = None,
    pods_by_step: dict[str, list] | None = None,
    drain_timeout: float | None = None,
    time_values: list[float] | None = None,
    return_extra: bool = False,
):
    """Run controller.main() with a mocked environment and watch stream.

    event_sequences: list of event batches, one per watch stream open() call.
    Each batch is exhausted before the next watch reconnect (if any).

    handlers / initial_handler_states / pods_by_step configure exit-handler
    dispatch (see TestHandlerDispatch). When return_extra is True, returns
    (result, mock_k8s, handler_state_snapshots) instead of just result;
    handler_state_snapshots is the list of dicts passed to
    _save_handler_states over the course of the run (last entry == final
    state).
    """
    env = {
        "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
        "SEEKR_CHAIN_NAMESPACE": "ns",
        "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
    }
    if drain_timeout is not None:
        env["SEEKR_CHAIN_HANDLER_DRAIN_TIMEOUT"] = str(drain_timeout)

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
    mock_k8s.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}
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
    if pods_by_step is not None:

        def _list_pods(namespace, label_selector):
            step = label_selector.rsplit("seekr-chain/step=", 1)[1]
            resp = MagicMock()
            resp.items = pods_by_step.get(step, [])
            return resp

        mock_core_v1.list_namespaced_pod.side_effect = _list_pods
    mock_core_v1_cls = MagicMock(return_value=mock_core_v1)

    def _load_manifest_mock(_assets, name):
        return {
            "metadata": {"name": f"{name}-js", "resourceVersion": "1"},
            "spec": {
                "replicatedJobs": [{"template": {"spec": {"template": {"spec": {"containers": [{"name": "main"}]}}}}}]
            },
        }

    # _load_phases: return persisted state if provided, otherwise all-PENDING
    def _load_phases_mock(_v1, _ns, _wid, dag):
        if initial_phases is not None:
            return dict(initial_phases)
        return {s["name"]: "PENDING" for s in dag}

    handler_state_snapshots: list[dict] = []

    def _save_handler_states_mock(_v1, _ns, _wid, states):
        handler_state_snapshots.append(dict(states))

    patches = [
        patch.dict("os.environ", env),
        patch.object(controller.kubernetes.config, "load_incluster_config"),
        patch.object(controller.kubernetes.client, "CustomObjectsApi", mock_custom_api_cls),
        patch.object(controller.kubernetes.client, "CoreV1Api", mock_core_v1_cls),
        patch.object(controller.kubernetes, "watch", MagicMock(Watch=mock_watch_cls)),
        patch.object(controller, "_load_manifest", side_effect=_load_manifest_mock),
        patch.object(controller, "_load_phases", side_effect=_load_phases_mock),
        patch.object(controller, "_save_phases"),
        patch.object(controller, "_load_handlers", return_value=dict(handlers or {})),
        patch.object(controller, "_load_handler_states", return_value=dict(initial_handler_states or {})),
        patch.object(controller, "_save_handler_states", side_effect=_save_handler_states_mock),
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
    ]
    if time_values is not None:
        patches.append(patch.object(controller.time, "time", side_effect=time_values))

    with contextlib.ExitStack() as stack:
        mock_emit_event = stack.enter_context(patch.object(controller, "_emit_event"))
        for p in patches:
            stack.enter_context(p)
        result = controller.main()

    if return_extra:
        return result, mock_k8s, handler_state_snapshots, mock_emit_event
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
        assert _run_main(dag, events) == 0

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

    def test_linear_step_b_fails_returns_0(self):
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
        assert _run_main(dag, events) == 0

    def test_step_a_failure_cascade_fails_b(self):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        # Only a-js fires; b should cascade-fail without being submitted
        events = [
            [_make_event("a-js", "Failed", rv="2")],
        ]
        assert _run_main(dag, events) == 0


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
        assert _run_main(dag, events) == 0


class TestMainCancellation:
    def test_single_step_cancelled_exits(self):
        """A JobSet suspended (chain cancel) with no terminalState must not hang."""
        dag = [{"name": "a", "depends_on": []}]
        events = [
            [_make_event("a-js", terminal=None, rv="2", suspend=True)],
        ]
        assert _run_main(dag, events) == 0

    def test_cascade_cancels_unsubmitted_dependent(self):
        """a is cancelled before b's dependency is satisfied — b must never be
        submitted and must cascade to CANCELLED instead of hanging."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        events = [
            [_make_event("a-js", terminal=None, rv="2", suspend=True)],
        ]
        assert _run_main(dag, events) == 0

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
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", terminal=None, rv="3", suspend=True),
                _make_event("c-js", "Completed", rv="4"),
            ],
        ]
        assert _run_main(dag, events) == 0


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
        mock_k8s.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}
        mock_k8s.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
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
            patch.object(controller, "_load_handlers", return_value={}),
            patch.object(controller, "_load_handler_states", return_value={}),
            patch.object(controller, "_save_handler_states"),
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
        mock_k8s.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}
        mock_k8s.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
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
            patch.object(controller, "_load_handlers", return_value={}),
            patch.object(controller, "_load_handler_states", return_value={}),
            patch.object(controller, "_save_handler_states"),
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
        mock_custom.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}
        mock_custom.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
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
            patch.object(controller, "_load_handlers", return_value={}),
            patch.object(controller, "_load_handler_states", return_value={}),
            patch.object(controller, "_save_handler_states"),
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


# ---------------------------------------------------------------------------
# Watch timeout (Fix 4) and transient submit retry (Fix 3)
# ---------------------------------------------------------------------------


class TestWatchTimeout:
    def test_stream_called_with_timeout_seconds(self):
        """w.stream() must be called with timeout_seconds to prevent stale heartbeat."""
        dag = [{"name": "a", "depends_on": []}]
        events = [
            [_make_event("a-js", "Completed", rv="2")],
        ]

        mock_watch_cls = MagicMock()
        mock_watch_instance = MagicMock()
        mock_watch_instance.stream.side_effect = lambda *a, **kw: (yield from events[0])
        mock_watch_instance.stop = MagicMock()
        mock_watch_cls.return_value = mock_watch_instance

        mock_k8s = MagicMock()
        mock_k8s.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}
        mock_k8s.create_namespaced_custom_object.return_value = {}

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
        }

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
            patch.object(controller, "_load_handlers", return_value={}),
            patch.object(controller, "_load_handler_states", return_value={}),
            patch.object(controller, "_save_handler_states"),
            patch.object(controller.json, "load", return_value=dag),
            patch("builtins.open", MagicMock(__enter__=lambda s, *a: s, __exit__=lambda s, *a: None)),
        ):
            result = controller.main()

        assert result == 0
        # Verify timeout_seconds was passed to w.stream()
        call_kwargs = mock_watch_instance.stream.call_args
        assert "timeout_seconds" in call_kwargs.kwargs
        assert call_kwargs.kwargs["timeout_seconds"] == 30


class TestTransientSubmitRetry:
    def test_step_retried_after_transient_error(self):
        """A 500 on submit leaves the step PENDING; on the next watch iteration
        the retry succeeds and the step completes."""
        dag = [{"name": "a", "depends_on": []}]

        call_count = [0]
        mock_k8s = MagicMock()
        mock_k8s.get_namespaced_custom_object.return_value = {"metadata": {"uid": "uid-123"}}

        def _create_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                from kubernetes.client.exceptions import ApiException

                raise ApiException(status=500)
            return {}

        mock_k8s.create_namespaced_custom_object.side_effect = _create_side_effect

        # First watch stream returns immediately (timeout, no events);
        # second delivers the completion event.
        stream_call_count = [0]

        def _stream_side_effect(*args, **kwargs):
            idx = stream_call_count[0]
            stream_call_count[0] += 1
            if idx == 0:
                return  # empty — simulates timeout return
            yield _make_event("a-js", "Completed", rv="2")

        mock_watch_cls = MagicMock()
        mock_watch_instance = MagicMock()
        mock_watch_instance.stream.side_effect = _stream_side_effect
        mock_watch_instance.stop = MagicMock()
        mock_watch_cls.return_value = mock_watch_instance

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": "/assets",
            "SEEKR_CHAIN_NAMESPACE": "ns",
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": "wf-abc",
        }

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
            patch.object(controller, "_load_handlers", return_value={}),
            patch.object(controller, "_load_handler_states", return_value={}),
            patch.object(controller, "_save_handler_states"),
            patch.object(controller.json, "load", return_value=dag),
            patch("builtins.open", MagicMock(__enter__=lambda s, *a: s, __exit__=lambda s, *a: None)),
        ):
            result = controller.main()

        assert result == 0
        # Submit was called twice: first failed, second succeeded
        assert call_count[0] == 2


# ---------------------------------------------------------------------------
# _load_handlers
# ---------------------------------------------------------------------------


class TestLoadHandlers:
    def test_groups_by_parent(self, tmp_path):
        entries = [
            {"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "always", "on_exit_codes": None},
            {"parent": "a", "name": "cleanup", "step": "a-eh-cleanup", "when": "on_failure", "on_exit_codes": None},
            {"parent": "b", "name": "notify", "step": "b-eh-notify", "when": "on_success", "on_exit_codes": None},
        ]
        (tmp_path / "handlers.json").write_text(json.dumps(entries))

        grouped = _load_handlers(str(tmp_path))

        assert [e["name"] for e in grouped["a"]] == ["notify", "cleanup"]
        assert [e["name"] for e in grouped["b"]] == ["notify"]

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert _load_handlers(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# _read_step_exit_info
# ---------------------------------------------------------------------------


class TestReadStepExitInfo:
    def test_picks_nonzero_exit_pod_over_success(self):
        pods = [
            _make_pod("a-0", exit_code=0, finished_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)),
            _make_pod(
                "a-1",
                exit_code=42,
                reason="Error",
                finished_at=datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=datetime.timezone.utc),
            ),
        ]
        core_v1 = _make_core_v1_for_pods(pods)

        info = _read_step_exit_info(core_v1, "ns", "wf-abc", "a")

        assert info["exit_code"] == 42
        assert info["reason"] == "Error"
        assert info["pod"] == "a-1"
        assert len(info["pod_exits"]) == 2

    def test_prefers_oom_killed_pod(self):
        pods = [
            _make_pod(
                "a-0",
                exit_code=1,
                reason="Error",
                finished_at=datetime.datetime(2026, 1, 1, 0, 0, 5, tzinfo=datetime.timezone.utc),
            ),
            _make_pod(
                "a-1",
                exit_code=137,
                reason="OOMKilled",
                finished_at=datetime.datetime(2026, 1, 1, 0, 0, 1, tzinfo=datetime.timezone.utc),
            ),
        ]
        core_v1 = _make_core_v1_for_pods(pods)

        info = _read_step_exit_info(core_v1, "ns", "wf-abc", "a")

        assert info["oom_killed"] is True
        assert info["pod"] == "a-1"

    def test_returns_default_when_list_pods_raises(self):
        from kubernetes.client.exceptions import ApiException

        core_v1 = _make_core_v1_for_pods([], raises=ApiException(status=500))

        info = _read_step_exit_info(core_v1, "ns", "wf-abc", "a")

        assert info == controller._default_exit_info()

    def test_returns_default_when_no_pods_found(self):
        core_v1 = _make_core_v1_for_pods([])

        info = _read_step_exit_info(core_v1, "ns", "wf-abc", "a")

        assert info == controller._default_exit_info()

    def test_uses_expected_label_selector(self):
        core_v1 = _make_core_v1_for_pods([])
        _read_step_exit_info(core_v1, "ns", "wf-abc", "a")
        core_v1.list_namespaced_pod.assert_called_once_with(
            namespace="ns",
            label_selector="seekr-chain/job-id=wf-abc,seekr-chain/step=a",
        )


# ---------------------------------------------------------------------------
# _handler_env
# ---------------------------------------------------------------------------


class TestHandlerEnv:
    def test_builds_full_env_list(self):
        handler_entry = {"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "on_failure"}
        exit_info = {
            "exit_code": 42,
            "reason": "Error",
            "message": "boom",
            "oom_killed": False,
            "pod": "a-1",
            "role": "worker",
            "pod_exits": [{"pod": "a-1", "role": "worker", "exit_code": 42}],
        }

        env = _handler_env(handler_entry, "a", "a-js", "FAILED", exit_info)

        assert env == [
            {"name": "SEEKR_CHAIN_HANDLER_NAME", "value": "notify"},
            {"name": "SEEKR_CHAIN_HANDLER_WHEN", "value": "on_failure"},
            {"name": "SEEKR_CHAIN_PARENT_STEP", "value": "a"},
            {"name": "SEEKR_CHAIN_PARENT_JOBSET", "value": "a-js"},
            {"name": "SEEKR_CHAIN_PARENT_STATUS", "value": "FAILED"},
            {"name": "SEEKR_CHAIN_PARENT_EXIT_CODE", "value": "42"},
            {"name": "SEEKR_CHAIN_PARENT_FAILURE_REASON", "value": "Error"},
            {"name": "SEEKR_CHAIN_PARENT_FAILURE_MESSAGE", "value": "boom"},
            {"name": "SEEKR_CHAIN_PARENT_OOM_KILLED", "value": "false"},
            {"name": "SEEKR_CHAIN_PARENT_POD", "value": "a-1"},
            {"name": "SEEKR_CHAIN_PARENT_ROLE", "value": "worker"},
            {
                "name": "SEEKR_CHAIN_PARENT_POD_EXITS",
                "value": json.dumps([{"pod": "a-1", "role": "worker", "exit_code": 42}]),
            },
        ]

    def test_none_exit_code_and_oom_true(self):
        handler_entry = {"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "always"}
        exit_info = controller._default_exit_info()
        exit_info["oom_killed"] = True

        env = _handler_env(handler_entry, "a", "a-js", "SUCCEEDED", exit_info)

        env_by_name = {e["name"]: e["value"] for e in env}
        assert env_by_name["SEEKR_CHAIN_PARENT_EXIT_CODE"] == ""
        assert env_by_name["SEEKR_CHAIN_PARENT_OOM_KILLED"] == "true"


# ---------------------------------------------------------------------------
# _inject_handler_env
# ---------------------------------------------------------------------------


class TestInjectHandlerEnv:
    def _manifest(self):
        return {
            "spec": {
                "replicatedJobs": [
                    {
                        "template": {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [
                                            {"name": "log-sidecar", "env": [{"name": "EXISTING", "value": "1"}]},
                                            {"name": "main", "env": [{"name": "EXISTING", "value": "1"}]},
                                        ],
                                    }
                                }
                            }
                        }
                    }
                ]
            }
        }

    def test_appends_only_to_main_container(self):
        manifest = self._manifest()
        env_entries = [{"name": "SEEKR_CHAIN_HANDLER_NAME", "value": "notify"}]

        _inject_handler_env(manifest, env_entries)

        containers = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"]
        sidecar = next(c for c in containers if c["name"] == "log-sidecar")
        main = next(c for c in containers if c["name"] == "main")

        assert sidecar["env"] == [{"name": "EXISTING", "value": "1"}]
        assert main["env"] == [
            {"name": "EXISTING", "value": "1"},
            {"name": "SEEKR_CHAIN_HANDLER_NAME", "value": "notify"},
        ]


# ---------------------------------------------------------------------------
# _load_handler_states / _save_handler_states
# ---------------------------------------------------------------------------


class TestHandlerStates:
    def test_round_trips_through_configmap(self):
        mock_v1 = MagicMock()
        cm = MagicMock()
        cm.data = {"phases": json.dumps({"a": "SUCCEEDED"}), "handlers": json.dumps({"a-eh-notify": "SUBMITTED"})}
        mock_v1.read_namespaced_config_map.return_value = cm

        states = _load_handler_states(mock_v1, "ns", "wf-abc")

        assert states == {"a-eh-notify": "SUBMITTED"}

    def test_submitted_restored_as_is_not_reset_to_pending(self):
        mock_v1 = MagicMock()
        cm = MagicMock()
        cm.data = {"handlers": json.dumps({"a-eh-notify": "SUBMITTED", "a-eh-cleanup": "PENDING"})}
        mock_v1.read_namespaced_config_map.return_value = cm

        states = _load_handler_states(mock_v1, "ns", "wf-abc")

        assert states["a-eh-notify"] == "SUBMITTED"
        assert states["a-eh-cleanup"] == "PENDING"

    def test_missing_configmap_returns_empty_dict(self):
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        mock_v1.read_namespaced_config_map.side_effect = ApiException(status=404)

        assert _load_handler_states(mock_v1, "ns", "wf-abc") == {}

    def test_save_does_not_clobber_phases_key(self):
        mock_v1 = MagicMock()

        _save_handler_states(mock_v1, "ns", "wf-abc", {"a-eh-notify": "SUBMITTED"})

        call = mock_v1.patch_namespaced_config_map.call_args
        assert call.kwargs["name"] == "wf-abc-phases"
        assert call.kwargs["body"] == {"data": {"handlers": json.dumps({"a-eh-notify": "SUBMITTED"})}}
        assert "phases" not in call.kwargs["body"]["data"]

    def test_save_api_error_does_not_raise(self):
        from kubernetes.client.exceptions import ApiException

        mock_v1 = MagicMock()
        mock_v1.patch_namespaced_config_map.side_effect = ApiException(status=500)

        # Should not raise
        _save_handler_states(mock_v1, "ns", "wf-abc", {"a-eh-notify": "SUBMITTED"})


# ---------------------------------------------------------------------------
# _workflow_settled
# ---------------------------------------------------------------------------


class TestWorkflowSettled:
    def test_true_when_all_terminal_and_no_handlers(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "SUCCEEDED"}
        assert _workflow_settled(dag, phases, {}, {}) is True

    def test_false_when_a_step_still_pending(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "PENDING"}
        assert _workflow_settled(dag, phases, {}, {}) is False

    def test_false_when_handler_pending_on_terminal_parent(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "SUCCEEDED"}
        handlers = {"a": [{"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "always"}]}
        handler_states = {}  # defaults to PENDING
        assert _workflow_settled(dag, phases, handlers, handler_states) is False

    def test_false_when_handler_submitted(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "SUCCEEDED"}
        handlers = {"a": [{"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "always"}]}
        handler_states = {"a-eh-notify": "SUBMITTED"}
        assert _workflow_settled(dag, phases, handlers, handler_states) is False

    def test_true_when_all_handlers_terminal(self):
        dag = [{"name": "a", "depends_on": []}]
        phases = {"a": "SUCCEEDED"}
        handlers = {"a": [{"parent": "a", "name": "notify", "step": "a-eh-notify", "when": "always"}]}
        handler_states = {"a-eh-notify": "SUCCEEDED"}
        assert _workflow_settled(dag, phases, handlers, handler_states) is True


# ---------------------------------------------------------------------------
# _submit_handlers_for_step / watch-loop handler dispatch (step 4b wiring)
# ---------------------------------------------------------------------------


def _handler_entry(parent="a", name="notify", when="always", on_exit_codes=None, step=None):
    return {
        "parent": parent,
        "name": name,
        "step": step or f"{parent}-eh-{name}",
        "when": when,
        "on_exit_codes": on_exit_codes,
    }


def _call_submit_handlers(
    step_name,
    phases,
    handlers,
    handler_states,
    js_names=None,
    existing_jobsets=None,
    pods=None,
):
    js_names = js_names or {}
    js_to_handler: dict[str, str] = {}
    mock_k8s = _make_k8s_custom([], existing_jobsets=existing_jobsets)
    mock_v1 = _make_core_v1_for_pods(pods or [])

    with patch.object(controller, "_load_manifest") as mock_load:
        mock_load.side_effect = lambda _assets, name: {
            "metadata": {"name": f"{name}-js"},
            "spec": {
                "replicatedJobs": [{"template": {"spec": {"template": {"spec": {"containers": [{"name": "main"}]}}}}}]
            },
        }
        _submit_handlers_for_step(
            step_name,
            phases,
            handlers,
            handler_states,
            js_names,
            js_to_handler,
            "/assets",
            "ns",
            "wf-abc",
            [],
            mock_k8s,
            mock_v1,
        )

    return handler_states, js_to_handler, mock_k8s


class TestHandlerDispatchGating:
    def test_on_success_fires_on_succeeded(self):
        handlers = {"a": [_handler_entry(when="on_success")]}
        handler_states: dict[str, str] = {}
        states, js_to_handler, mock_k8s = _call_submit_handlers("a", {"a": "SUCCEEDED"}, handlers, handler_states)
        assert states["a-eh-notify"] == "SUBMITTED"
        assert js_to_handler == {"a-eh-notify-js": "a-eh-notify"}
        mock_k8s.create_namespaced_custom_object.assert_called_once()

    def test_on_success_does_not_fire_on_failed(self):
        handlers = {"a": [_handler_entry(when="on_success")]}
        handler_states: dict[str, str] = {}
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "FAILED"}, handlers, handler_states)
        assert states["a-eh-notify"] == "SKIPPED"
        mock_k8s.create_namespaced_custom_object.assert_not_called()

    def test_on_failure_fires_on_failed_not_succeeded(self):
        handlers = {"a": [_handler_entry(when="on_failure")]}

        failed_states, _, failed_k8s = _call_submit_handlers("a", {"a": "FAILED"}, handlers, {})
        assert failed_states["a-eh-notify"] == "SUBMITTED"
        failed_k8s.create_namespaced_custom_object.assert_called_once()

        succeeded_states, _, succeeded_k8s = _call_submit_handlers("a", {"a": "SUCCEEDED"}, handlers, {})
        assert succeeded_states["a-eh-notify"] == "SKIPPED"
        succeeded_k8s.create_namespaced_custom_object.assert_not_called()

    def test_always_fires_on_both_success_and_failure(self):
        handlers = {"a": [_handler_entry(when="always")]}

        succeeded_states, _, _ = _call_submit_handlers("a", {"a": "SUCCEEDED"}, handlers, {})
        assert succeeded_states["a-eh-notify"] == "SUBMITTED"

        failed_states, _, _ = _call_submit_handlers("a", {"a": "FAILED"}, handlers, {})
        assert failed_states["a-eh-notify"] == "SUBMITTED"

    def test_cancelled_skips_all_handlers_regardless_of_when(self):
        handlers = {
            "a": [
                _handler_entry(name="always-h", when="always"),
                _handler_entry(name="success-h", when="on_success"),
                _handler_entry(name="failure-h", when="on_failure"),
            ]
        }
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "CANCELLED"}, handlers, {})
        assert states["a-eh-always-h"] == "SKIPPED"
        assert states["a-eh-success-h"] == "SKIPPED"
        assert states["a-eh-failure-h"] == "SKIPPED"
        mock_k8s.create_namespaced_custom_object.assert_not_called()

    def test_on_exit_codes_fires_when_exit_code_matches(self):
        handlers = {"a": [_handler_entry(when="always", on_exit_codes=[42])]}
        pods = [_make_pod("a-0", exit_code=42, reason="Error")]
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "FAILED"}, handlers, {}, pods=pods)
        assert states["a-eh-notify"] == "SUBMITTED"
        mock_k8s.create_namespaced_custom_object.assert_called_once()

    def test_on_exit_codes_skips_when_exit_code_does_not_match(self):
        handlers = {"a": [_handler_entry(when="always", on_exit_codes=[42])]}
        pods = [_make_pod("a-0", exit_code=1, reason="Error")]
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "FAILED"}, handlers, {}, pods=pods)
        assert states["a-eh-notify"] == "SKIPPED"
        mock_k8s.create_namespaced_custom_object.assert_not_called()

    def test_no_handlers_for_step_is_a_noop(self):
        states, js_to_handler, mock_k8s = _call_submit_handlers("a", {"a": "SUCCEEDED"}, {}, {})
        assert states == {}
        assert js_to_handler == {}
        mock_k8s.create_namespaced_custom_object.assert_not_called()

    def test_already_terminal_handler_state_is_not_resubmitted(self):
        handlers = {"a": [_handler_entry(when="always")]}
        handler_states = {"a-eh-notify": "SUCCEEDED"}
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "SUCCEEDED"}, handlers, handler_states)
        assert states["a-eh-notify"] == "SUCCEEDED"
        mock_k8s.create_namespaced_custom_object.assert_not_called()

    def test_409_on_submit_marks_submitted_without_crashing(self):
        """Pre-seeded PENDING + a JobSet that already exists (prior partial submit):
        the 409 guard treats it as already running, same as _submit_ready_steps."""
        handlers = {"a": [_handler_entry(when="always")]}
        states, js_to_handler, mock_k8s = _call_submit_handlers(
            "a", {"a": "SUCCEEDED"}, handlers, {}, existing_jobsets=["a-eh-notify-js"]
        )
        assert states["a-eh-notify"] == "SUBMITTED"
        assert js_to_handler == {"a-eh-notify-js": "a-eh-notify"}

    def test_missing_exit_info_degrades_gracefully_but_still_submits(self):
        """on_exit_codes is None -> the env-var mechanism is primary; a handler
        with no gate still fires even though _read_step_exit_info returns the
        all-empty default (e.g. pods already GC'd)."""
        handlers = {"a": [_handler_entry(when="on_failure")]}
        states, _, mock_k8s = _call_submit_handlers("a", {"a": "FAILED"}, handlers, {}, pods=[])
        assert states["a-eh-notify"] == "SUBMITTED"
        body = mock_k8s.create_namespaced_custom_object.call_args.kwargs["body"]
        containers = body["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]["containers"]
        main = next(c for c in containers if c["name"] == "main")
        env_by_name = {e["name"]: e["value"] for e in main["env"]}
        assert env_by_name["SEEKR_CHAIN_PARENT_EXIT_CODE"] == ""
        assert env_by_name["SEEKR_CHAIN_PARENT_FAILURE_REASON"] == ""


class TestHandlerDispatchIntegration:
    """End-to-end via controller.main() with a mocked watch stream."""

    def test_diamond_dag_handler_failure_does_not_cascade(self):
        """a -> b; a has an 'always' handler that itself fails. b must still run
        and the run must not be affected by the handler's failure."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        handlers = {"a": [_handler_entry(when="always")]}
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("a-eh-notify-js", "Failed", rv="3"),
                _make_event("b-js", "Completed", rv="4"),
            ],
        ]

        result, mock_k8s, snapshots, mock_emit_event = _run_main(dag, events, handlers=handlers, return_extra=True)

        assert result == 0
        assert snapshots[-1]["a-eh-notify"] == "FAILED"

        submitted = [
            call.kwargs["body"]["metadata"]["name"] for call in mock_k8s.create_namespaced_custom_object.call_args_list
        ]
        assert "b-js" in submitted  # downstream step still ran

        reasons = [call.args[4] for call in mock_emit_event.call_args_list]
        assert "WorkflowFailed" not in reasons
        assert "HandlerFailed" in reasons

    def test_env_injection_from_mocked_pod_status_reaches_handler_pod(self):
        dag = [{"name": "a", "depends_on": []}]
        handlers = {"a": [_handler_entry(when="on_failure")]}
        events = [
            [_make_event("a-js", "Failed", rv="2")],
            [_make_event("a-eh-notify-js", "Completed", rv="3")],
        ]
        pods_by_step = {"a": [_make_pod("a-0", exit_code=137, reason="OOMKilled", message="killed: OOM")]}

        result, mock_k8s, snapshots, _ = _run_main(
            dag, events, handlers=handlers, pods_by_step=pods_by_step, return_extra=True
        )

        assert result == 0
        assert snapshots[-1]["a-eh-notify"] == "SUCCEEDED"

        handler_call = next(
            call
            for call in mock_k8s.create_namespaced_custom_object.call_args_list
            if call.kwargs["body"]["metadata"]["name"] == "a-eh-notify-js"
        )
        containers = handler_call.kwargs["body"]["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"][
            "containers"
        ]
        main = next(c for c in containers if c["name"] == "main")
        env_by_name = {e["name"]: e["value"] for e in main["env"]}
        assert env_by_name["SEEKR_CHAIN_PARENT_EXIT_CODE"] == "137"
        assert env_by_name["SEEKR_CHAIN_PARENT_OOM_KILLED"] == "true"
        assert env_by_name["SEEKR_CHAIN_PARENT_FAILURE_MESSAGE"] == "killed: OOM"

    def test_controller_does_not_settle_until_handler_terminates(self):
        """First watch stream delivers only the step's completion; the handler
        JobSet's own terminal event arrives on the reconnect. main() must not
        return until that second event lands."""
        dag = [{"name": "a", "depends_on": []}]
        handlers = {"a": [_handler_entry(when="always")]}
        events = [
            [_make_event("a-js", "Completed", rv="2")],
            [_make_event("a-eh-notify-js", "Completed", rv="3")],
        ]

        result, _, snapshots, _ = _run_main(dag, events, handlers=handlers, return_extra=True)

        assert result == 0
        assert snapshots[-1]["a-eh-notify"] == "SUCCEEDED"

    def test_drain_timeout_breaks_the_wait(self):
        """A handler is SUBMITTED but never reports a terminal watch event.
        Once the drain timeout elapses after the step went terminal, the
        controller must give up rather than hang forever."""
        dag = [{"name": "a", "depends_on": []}]
        handlers = {"a": [_handler_entry(when="always")]}
        events = [
            [_make_event("a-js", "Completed", rv="2")],
            [],
            [],
        ]

        result, _, snapshots, mock_emit_event = _run_main(
            dag,
            events,
            handlers=handlers,
            drain_timeout=10,
            time_values=[0, 1, 2, 3, 100, 100, 100, 100, 100, 100, 100, 100],
            return_extra=True,
        )

        assert result == 0
        assert snapshots[-1]["a-eh-notify"] == "SUBMITTED"
        reasons = [call.args[4] for call in mock_emit_event.call_args_list]
        assert "HandlerDrainTimeout" in reasons

    def test_restart_idempotency_pre_seeded_submitted_plus_409_no_double_injection(self):
        """Controller restarted after a handler was already submitted: state is
        restored as SUBMITTED (not reset to PENDING), and a redundant dispatch
        attempt would hit 409 rather than double-injecting env or crashing."""
        dag = [{"name": "a", "depends_on": []}]
        handlers = {"a": [_handler_entry(when="always")]}
        events = [[_make_event("a-eh-notify-js", "Completed", rv="2")]]

        result, mock_k8s, snapshots, _ = _run_main(
            dag,
            events,
            handlers=handlers,
            initial_phases={"a": "SUCCEEDED"},
            initial_handler_states={"a-eh-notify": "SUBMITTED"},
            existing_jobsets=["a-eh-notify-js"],
            return_extra=True,
        )

        assert result == 0
        # Restored as SUBMITTED means _submit_handlers_for_step treats it as
        # already dispatched and never calls create_namespaced_custom_object
        # for it again.
        assert mock_k8s.create_namespaced_custom_object.call_count == 0
        assert snapshots[-1]["a-eh-notify"] == "SUCCEEDED"

    def test_old_assets_without_handlers_json_behave_as_before(self):
        """handlers.json missing -> _load_handlers returns {} -> no handler
        activity at all; a plain linear DAG completes exactly as pre-feature."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        events = [
            [
                _make_event("a-js", "Completed", rv="2"),
                _make_event("b-js", "Completed", rv="3"),
            ],
        ]

        result, mock_k8s, snapshots, _ = _run_main(dag, events, handlers={}, return_extra=True)

        assert result == 0
        assert all(s == {} for s in snapshots)  # no handler ever entered a non-empty state
        submitted = [
            call.kwargs["body"]["metadata"]["name"] for call in mock_k8s.create_namespaced_custom_object.call_args_list
        ]
        assert submitted == ["a-js", "b-js"]
