"""Unit tests for the controller DAG executor (resources/controller package).

The controller package runs inside the controller pod and has no seekr_chain
dependency, so we put the resources dir on sys.path and import the package
modules directly, avoiding any packaging side effects (e.g. seekr_chain/__init__
pulling in boto3/kubernetes at import time).
"""

import json
import os
import sys
import tempfile
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

from controller import phases, scheduling, status, watch  # noqa: E402

from .fake_k8s import FakeK8sCluster  # noqa: E402

_workflow_status_from_phases = status._workflow_status_from_phases
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


def _record_status_call(cluster: FakeK8sCluster, workflow_id, dag, phases, timings) -> None:
    """side_effect for watch.write_status/watch.flush_status: status.py's
    file write + S3 ship have no place in an in-memory fake, so instead
    record the workflow-level status onto cluster.trace (deduped against the
    last-recorded status), derived the same way the real status document
    derives it — including excluding `optional` steps from the rollup."""
    optional_steps = {step["name"] for step in dag if step.get("optional", False)}
    cluster.record_status(_workflow_status_from_phases(phases, optional_steps))


def _record_ship_call(cluster: FakeK8sCluster, argv: list[str], **kwargs) -> MagicMock:
    """side_effect for status.subprocess.run in capture_status mode: records
    the s5cmd argv the real _ship_once() would have invoked onto
    cluster.ship_calls instead of actually invoking it."""
    cluster.ship_calls.append(argv)
    return MagicMock(returncode=0)


def run_controller_main(
    cluster: FakeK8sCluster,
    dag: list[dict],
    *,
    job_name: str = "wf-abc",
    namespace: str = "ns",
    assets_path: str = "/assets",
    capture_status: bool = False,
) -> int:
    """Patch kubernetes.config/client/watch to `cluster`, patch open()/json.load
    for dag.json and write_status/flush_status to record onto cluster.trace,
    set env vars, call controller.watch.main(), return its result.

    capture_status=True instead exercises the real status.py doc-build +
    file-write + s5cmd-ship path end to end (see
    _run_controller_main_capturing_status) and leaves the parsed status.json
    on cluster.status_doc; the default False path is unchanged.
    """
    if job_name not in cluster.jobsets:
        cluster.set_controller_jobset(job_name, "uid-123")

    if capture_status:
        return _run_controller_main_capturing_status(cluster, dag, job_name=job_name, namespace=namespace)

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
        patch.object(watch, "write_status", side_effect=partial(_record_status_call, cluster)),
        patch.object(watch, "flush_status", side_effect=partial(_record_status_call, cluster)),
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

    return result


def _run_controller_main_capturing_status(
    cluster: FakeK8sCluster,
    dag: list[dict],
    *,
    job_name: str,
    namespace: str,
) -> int:
    """capture_status=True path for run_controller_main: unlike the default
    path, write_status/flush_status and dag.json's open()/json.load are left
    real, so the actual status.py doc-build + file-write + s5cmd-ship runs
    through main() instead of being mocked onto cluster.trace. The
    background shipper is stubbed to a no-op — otherwise the async
    write_status() calls would spin up a real daemon thread — so only the
    synchronous terminal flush_status() ship is observed, which is
    deterministic. s5cmd itself is replaced by a recorder onto
    cluster.ship_calls.
    """
    cluster.ship_calls = []

    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "dag.json"), "w") as f:
            json.dump(dag, f)
        status_path = os.path.join(tmpdir, "status.json")

        env = {
            "SEEKR_CHAIN_JOB_ASSET_PATH": tmpdir,
            "SEEKR_CHAIN_NAMESPACE": namespace,
            "SEEKR_CHAIN_CONTROLLER_JOB_NAME": job_name,
            "SEEKR_CHAIN_REMOTE_STATUS_PATH": "s3://bucket/wf-abc/status.json",
        }

        with (
            patch.dict("os.environ", env),
            patch.object(watch.kubernetes.config, "load_incluster_config"),
            patch.object(watch.kubernetes.client, "CustomObjectsApi", cluster.custom_objects_api),
            patch.object(watch.kubernetes.client, "CoreV1Api", cluster.core_v1_api),
            patch.object(watch.kubernetes.watch, "Watch", cluster.watch),
            patch.object(scheduling, "load_manifest", side_effect=_load_manifest_mock),
            patch.object(watch.time, "sleep"),
            patch.object(status, "_STATUS_PATH", status_path),
            patch.object(status, "_ensure_shipper"),
            patch.object(status.subprocess, "run", side_effect=partial(_record_ship_call, cluster)),
        ):
            result = watch.main()

        with open(status_path) as f:
            cluster.status_doc = json.load(f)
        cluster.status_path = status_path

    return result


class TestDeriveStatus:
    @pytest.mark.parametrize(
        "phases, expected",
        [
            ({"a": "CANCELED", "b": "FAILED"}, "FAILED"),
            ({"a": "CANCELED", "b": "SUCCEEDED"}, "CANCELED"),
            ({"a": "FAILED", "b": "SUCCEEDED"}, "FAILED"),
            ({"a": "SUCCEEDED", "b": "SKIPPED"}, "SUCCEEDED"),
            ({"a": "SUCCEEDED", "b": "RUNNING"}, "RUNNING"),
            ({"a": "PENDING", "b": "PENDING"}, "PENDING"),
        ],
    )
    def test_precedence(self, phases, expected):
        assert _workflow_status_from_phases(phases) == expected


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


class TestNormalizeDep:
    def test_bare_string_becomes_on_success_condition(self):
        assert phases.normalize_dep("a") == {
            "step": "a",
            "when": "ON_SUCCESS",
            "on_exit_codes": None,
            "operator": "IN",
        }

    def test_dict_entry_is_defaulted_then_overridden(self):
        assert phases.normalize_dep({"step": "a", "when": "ON_FAILURE"}) == {
            "step": "a",
            "when": "ON_FAILURE",
            "on_exit_codes": None,
            "operator": "IN",
        }


class TestDepSatisfied:
    """Pure truth table over (phase, when, on_exit_codes, operator, target's
    captured exit codes) — no I/O and no loop semantics, so a table test is
    clearer here than driving main() once per row."""

    @pytest.mark.parametrize(
        "phase, cond, exit_codes, expected",
        [
            # ON_SUCCESS: only a SUCCEEDED dependency satisfies it.
            ("SUCCEEDED", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("FAILED", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            ("CANCELED", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            ("SKIPPED", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            ("PENDING", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            ("RUNNING", {"when": "ON_SUCCESS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            # ALWAYS: any terminal phase satisfies it, but not a non-terminal one.
            ("SUCCEEDED", {"when": "ALWAYS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("FAILED", {"when": "ALWAYS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("CANCELED", {"when": "ALWAYS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("SKIPPED", {"when": "ALWAYS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("RUNNING", {"when": "ALWAYS", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            # ON_FAILURE, no exit-code gate: FAILED/CANCELLED/SKIPPED all satisfy it, SUCCEEDED doesn't.
            ("FAILED", {"when": "ON_FAILURE", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("CANCELED", {"when": "ON_FAILURE", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("SKIPPED", {"when": "ON_FAILURE", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, True),
            ("SUCCEEDED", {"when": "ON_FAILURE", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            ("RUNNING", {"when": "ON_FAILURE", "on_exit_codes": None, "operator": "IN", "step": "a"}, {}, False),
            # ON_FAILURE with an IN exit-code gate.
            (
                "FAILED",
                {"when": "ON_FAILURE", "on_exit_codes": [42], "operator": "IN", "step": "a"},
                {"a": [42]},
                True,
            ),
            (
                "FAILED",
                {"when": "ON_FAILURE", "on_exit_codes": [42], "operator": "IN", "step": "a"},
                {"a": [7]},
                False,
            ),
            # ON_FAILURE with a NOT_IN exit-code gate (inverted match).
            (
                "FAILED",
                {"when": "ON_FAILURE", "on_exit_codes": [42], "operator": "NOT_IN", "step": "a"},
                {"a": [7]},
                True,
            ),
            (
                "FAILED",
                {"when": "ON_FAILURE", "on_exit_codes": [42], "operator": "NOT_IN", "step": "a"},
                {"a": [42]},
                False,
            ),
            # A CANCELLED target never got its exit code captured — an IN gate
            # can't match an empty exit-codes list.
            (
                "CANCELED",
                {"when": "ON_FAILURE", "on_exit_codes": [42], "operator": "IN", "step": "a"},
                {},
                False,
            ),
        ],
    )
    def test_truth_table(self, phase, cond, exit_codes, expected):
        assert phases.dep_satisfied(phase, cond, exit_codes) is expected


# ---------------------------------------------------------------------------
# main() — end-to-end DAG execution via mocked watch stream
# ---------------------------------------------------------------------------


@pytest.fixture
def cluster():
    return FakeK8sCluster()


class TestMainLinearDag:
    def test_single_step_success(self, cluster):
        dag = [{"name": "a", "depends_on": []}]
        cluster.script_step("a", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_single_step_failure(self, cluster):
        dag = [{"name": "a", "depends_on": []}]
        cluster.script_step("a", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_linear_two_steps(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        # b is only submitted once a has succeeded.
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_linear_step_b_fails_returns_0(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['b']"),
        ]

    def test_step_a_failure_cascade_fails_b(self, cluster):
        """Only a-js fires; b should cascade-fail without being submitted."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.script_step("a", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        # b must never be submitted — a's failure cascade-skips it.
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "SKIPPED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]
        assert ("submit", "b") not in cluster.trace

    def test_transitive_cascade_skips_both_downstream_steps(self, cluster):
        """a→b→c; only a is scripted (fails). b and c must both cascade to
        SKIPPED in a single pass — neither is ever submitted."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["b"]},
        ]
        cluster.script_step("a", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "SKIPPED", "c": "SKIPPED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]
        assert ("submit", "b") not in cluster.trace
        assert ("submit", "c") not in cluster.trace


class TestMainDiamondDag:
    def test_diamond_all_succeed(self, cluster):
        """a → b, a → c, b+c → d."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)
        cluster.script_step("c", exit_code=0)
        cluster.script_step("d", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("submit", "c"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "RUNNING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED", "c": "RUNNING", "d": "PENDING"}),
            ("exit", "c", 0),
            ("event", "StepSucceeded", "Step 'c' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED", "c": "SUCCEEDED", "d": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "d"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED", "c": "SUCCEEDED", "d": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "d", 0),
            ("event", "StepSucceeded", "Step 'd' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED", "c": "SUCCEEDED", "d": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_diamond_b_fails_d_cascade_fails(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        # d must never be submitted — it cascade-skips once b fails.
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("submit", "c"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "RUNNING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "RUNNING", "d": "SKIPPED"}),
            ("status", "FAILED"),
            ("exit", "c", 0),
            ("event", "StepSucceeded", "Step 'c' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "SUCCEEDED", "d": "SKIPPED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['b']"),
        ]
        assert ("submit", "d") not in cluster.trace


class TestMainCancellation:
    def test_single_step_cancelled_exits(self, cluster):
        """A JobSet suspended (chain cancel) with no terminalState must not hang."""
        dag = [{"name": "a", "depends_on": []}]
        cluster.script_step("a", cancel=True)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("cancel", "a"),
            ("event", "StepCancelled", "Step 'a' was cancelled"),
            ("phases", {"a": "CANCELED"}),
            ("status", "CANCELED"),
            ("event", "WorkflowCancelled", "Workflow cancelled — cancelled steps: ['a']"),
        ]

    def test_cascade_cancels_unsubmitted_dependent(self, cluster):
        """a is cancelled before b's dependency is satisfied — b must never be
        submitted and must cascade-skip instead of hanging."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.script_step("a", cancel=True)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("cancel", "a"),
            ("event", "StepCancelled", "Step 'a' was cancelled"),
            ("phases", {"a": "CANCELED", "b": "SKIPPED"}),
            ("status", "CANCELED"),
            ("event", "WorkflowCancelled", "Workflow cancelled — cancelled steps: ['a']"),
        ]
        assert ("submit", "b") not in cluster.trace

    def test_diamond_partial_cancel_cascades_join_step(self, cluster):
        """a → b, a → c, b+c → d. b is cancelled, c succeeds — d must
        cascade-skip rather than waiting forever for a JobSet that is never
        submitted.

        Kept on manual complete_jobset/cancel_jobset rather than scripted
        cancellation: all three of a/b/c must be pre-enqueued *before* the
        controller starts, so they land in the watch queue together and
        js_to_step catches each mid-drain via 409-resume. script_step only
        enqueues its event at create time — mixed with an
        immediately-enqueued cancel, that can reorder relative to when the
        controller starts tracking a JobSet and drop the cancel event.
        """
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster.complete_jobset("a-js")
        cluster.cancel_jobset("b-js")
        cluster.complete_jobset("c-js")
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "PENDING"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "RUNNING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("cancel", "b"),
            ("event", "StepCancelled", "Step 'b' was cancelled"),
            ("phases", {"a": "SUCCEEDED", "b": "CANCELED", "c": "RUNNING", "d": "SKIPPED"}),
            ("status", "CANCELED"),
            ("exit", "c", 0),
            ("event", "StepSucceeded", "Step 'c' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "CANCELED", "c": "SUCCEEDED", "d": "SKIPPED"}),
            ("event", "WorkflowCancelled", "Workflow cancelled — cancelled steps: ['b']"),
        ]


class TestMainSkippedStatus:
    def test_workflow_failed_event_excludes_skipped_steps(self, cluster):
        """a → b, a → c, b+c → d; only b fails. The WorkflowFailed event's
        failed-steps list must contain only b — d is SKIPPED (never ran), not
        FAILED, so it must not be reported as a failed step."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "PENDING", "d": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("submit", "c"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "RUNNING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "RUNNING", "d": "SKIPPED"}),
            ("status", "FAILED"),
            ("exit", "c", 0),
            ("event", "StepSucceeded", "Step 'c' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "SUCCEEDED", "d": "SKIPPED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['b']"),
        ]
        # d is SKIPPED (never ran), not FAILED — it must not appear in the
        # WorkflowFailed event's failed-steps list, and the event must fire
        # exactly once.
        failed_events = [t for t in cluster.trace if t[0] == "event" and t[1] == "WorkflowFailed"]
        assert len(failed_events) == 1


class TestMainWatchReconnect:
    def test_reconnects_after_generic_exception(self, cluster):
        """Watch stream raises an exception; controller reconnects and completes."""
        dag = [{"name": "a", "depends_on": []}]
        cluster.raise_on_next_stream(Exception("transient network error"))
        cluster.script_step("a", exit_code=0)

        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_reconnects_after_410_gone(self, cluster):
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
        cluster.raise_on_next_stream(ApiException(status=410))
        cluster.script_step("a", exit_code=0)

        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]


class TestMainControllerRetry:
    def test_409_on_submit_treated_as_resume(self, cluster):
        """Controller pod restarted: JobSet already exists (409). Should resume, not crash.

        submit_jobset + complete_jobset (not script_step) — the JobSet must
        exist *before* the controller's create call, which script_step can't
        express since it only resolves outcomes at create time.
        """
        dag = [{"name": "a", "depends_on": []}]
        cluster.submit_jobset("a-js")
        cluster.complete_jobset("a-js")
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_multi_step_partial_resume(self, cluster):
        """Controller restarts after step a was already submitted but not yet complete.
        Step b has not been submitted yet."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.submit_jobset("a-js")
        cluster.complete_jobset("a-js")
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_configmap_resume_does_not_resubmit_completed_step(self, cluster):
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
        cluster.configmaps["wf-abc-phases"] = {
            "data": {
                "phases": json.dumps({"a": "SUCCEEDED", "b": "PENDING"}),
                "timings": json.dumps({}),
            }
        }
        cluster.script_step("b", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        # a's JobSet must never be submitted — it was already done before the restart
        assert "a-js" not in cluster.jobsets
        assert "b-js" in cluster.jobsets
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_restart_ignores_unknown_and_stale_step_names(self, cluster):
        """ConfigMap carries a stale 'zombie' step name that no longer exists
        in the DAG — load_phases must drop it silently rather than choking or
        submitting a 'zombie-js' JobSet."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.configmaps["wf-abc-phases"] = {
            "data": {
                "phases": json.dumps({"a": "SUCCEEDED", "b": "PENDING", "zombie": "SUCCEEDED"}),
                "timings": json.dumps({}),
            }
        }
        cluster.script_step("b", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        assert "a-js" not in cluster.jobsets
        assert all("zombie" not in name for name in cluster.jobsets)
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]


class TestWatchTimeout:
    def test_stream_called_with_timeout_seconds(self, cluster):
        """w.stream() must be called with timeout_seconds to prevent stale heartbeat."""
        dag = [{"name": "a", "depends_on": []}]
        cluster.script_step("a", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.watch_last_kwargs["timeout_seconds"] == 30


# ---------------------------------------------------------------------------
# Transient submit retry (Fix 3)
# ---------------------------------------------------------------------------


class TestTransientSubmitRetry:
    def test_step_retried_after_transient_error(self, cluster):
        """A 500 on submit leaves the step PENDING; on the next watch iteration
        the retry succeeds and the step completes."""
        dag = [{"name": "a", "depends_on": []}]
        cluster.fail_next_create(500)
        cluster.script_step("a", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        # Submit was attempted twice: first failed, second succeeded. The
        # failed attempt raises before recording, so only the successful
        # submit shows up in the trace.
        assert cluster.create_attempts == 2
        assert cluster.trace == [
            ("status", "PENDING"),
            ("phases", {"a": "PENDING"}),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]


class TestMainSubmitError:
    def test_permanent_submit_error_fails_step_and_cascades(self, cluster):
        """A 403 on submit is permanent: a is marked FAILED without ever
        recording a submit (create raises before the fake records it), b
        cascade-skips, and the workflow fails."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.fail_next_create(403)

        assert run_controller_main(cluster, dag) == 0

        assert cluster.trace == [
            ("status", "PENDING"),
            ("phases", {"a": "FAILED", "b": "PENDING"}),
            ("status", "FAILED"),
            ("phases", {"a": "FAILED", "b": "SKIPPED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]
        assert not any(t[0] == "submit" for t in cluster.trace)


# ---------------------------------------------------------------------------
# End-to-end status.json (real status.py doc-build + file-write + s5cmd-ship,
# via run_controller_main(..., capture_status=True))
# ---------------------------------------------------------------------------


def _pop_dynamic_status_fields(doc: dict) -> dict:
    """Pop status.json's real-timestamp fields (top-level captured_at, each
    step's dt_start/dt_end) off `doc` in place, so the remainder can be
    asserted with a single equality on a literal. Returns the popped values
    for the caller to assert shape on separately."""
    captured_at = doc.pop("captured_at")
    dt_by_step = {step["name"]: (step.pop("dt_start"), step.pop("dt_end")) for step in doc["steps"]}
    return {"captured_at": captured_at, "dt_by_step": dt_by_step}


class TestMainStatusJson:
    def test_success(self, cluster):
        """Linear a -> b, both succeed: the real status.json ends up
        SUCCEEDED with both steps SUCCEEDED and timestamped, and the
        terminal flush_status() ships it."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)

        assert run_controller_main(cluster, dag, capture_status=True) == 0

        dynamic = _pop_dynamic_status_fields(cluster.status_doc)
        assert dynamic["captured_at"] is not None
        for name in ("a", "b"):
            dt_start, dt_end = dynamic["dt_by_step"][name]
            assert dt_start is not None
            assert dt_end is not None
        assert cluster.status_doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "SUCCEEDED",
            "steps": [
                {"name": "a", "phase": "SUCCEEDED"},
                {"name": "b", "phase": "SUCCEEDED"},
            ],
        }
        assert ["s5cmd", "cp", cluster.status_path, "s3://bucket/wf-abc/status.json"] in cluster.ship_calls

    def test_failure_and_skip(self, cluster):
        """Diamond a -> b, a -> c, b+c -> d; b fails. d is SKIPPED (never
        ran) and must have no dt_start/dt_end, unlike a/b/c which all ran."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": ["a"]},
            {"name": "c", "depends_on": ["a"]},
            {"name": "d", "depends_on": ["b", "c"]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)

        assert run_controller_main(cluster, dag, capture_status=True) == 0

        dynamic = _pop_dynamic_status_fields(cluster.status_doc)
        for name in ("a", "b", "c"):
            dt_start, dt_end = dynamic["dt_by_step"][name]
            assert dt_start is not None
            assert dt_end is not None
        assert dynamic["dt_by_step"]["d"] == (None, None)
        assert cluster.status_doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "FAILED",
            "steps": [
                {"name": "a", "phase": "SUCCEEDED"},
                {"name": "b", "phase": "FAILED"},
                {"name": "c", "phase": "SUCCEEDED"},
                {"name": "d", "phase": "SKIPPED"},
            ],
        }
        assert ["s5cmd", "cp", cluster.status_path, "s3://bucket/wf-abc/status.json"] in cluster.ship_calls

    def test_cancellation(self, cluster):
        """A single cancelled step ends the workflow CANCELED with the
        step's phase CANCELED, and still ships the terminal status."""
        dag = [{"name": "a", "depends_on": []}]
        cluster.script_step("a", cancel=True)

        assert run_controller_main(cluster, dag, capture_status=True) == 0

        dynamic = _pop_dynamic_status_fields(cluster.status_doc)
        assert dynamic["captured_at"] is not None
        assert cluster.status_doc == {
            "schema_version": 1,
            "id": "wf-abc",
            "status": "CANCELED",
            "steps": [{"name": "a", "phase": "CANCELED"}],
        }
        assert ["s5cmd", "cp", cluster.status_path, "s3://bucket/wf-abc/status.json"] in cluster.ship_calls


# ---------------------------------------------------------------------------
# Conditional depends_on: ON_FAILURE / ALWAYS edges, exit-code gating, and
# fail-fast teardown (a failed step cancels other RUNNING steps, skips other
# PENDING steps, and waits only for direct reactive dependents).
# ---------------------------------------------------------------------------


class TestMainConditionalDependsOn:
    def test_on_failure_step_runs_after_target_fails(self, cluster):
        """b is an ON_FAILURE cleanup step for a: a fails, b still gets
        submitted and runs to completion instead of cascade-skipping."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ON_FAILURE"}]},
        ]
        cluster.script_step("a", exit_code=1)
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "b"),
            ("phases", {"a": "FAILED", "b": "RUNNING"}),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "FAILED", "b": "SUCCEEDED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_on_failure_step_skipped_as_dead_end_when_target_succeeds(self, cluster):
        """b only fires ON_FAILURE of a; a succeeds, so b's condition can
        never be satisfied and it must be marked SKIPPED rather than hang
        PENDING forever."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ON_FAILURE"}]},
        ]
        cluster.script_step("a", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SKIPPED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_always_step_runs_when_target_succeeds(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_always_step_runs_when_target_fails(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}]},
        ]
        cluster.script_step("a", exit_code=1)
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "b"),
            ("phases", {"a": "FAILED", "b": "RUNNING"}),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "FAILED", "b": "SUCCEEDED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_on_failure_exit_code_gate_matches_runs_step(self, cluster):
        """b is gated on a's exit code being 42; a fails with exactly that
        code (exposed via its pod's terminated exit code), so b runs."""
        dag = [
            {"name": "a", "depends_on": []},
            {
                "name": "b",
                "depends_on": [{"step": "a", "when": "ON_FAILURE", "on_exit_codes": [42]}],
            },
        ]
        cluster.script_step("a", exit_code=1, pods=[{"role": "worker", "exit_code": 42}])
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "b"),
            ("phases", {"a": "FAILED", "b": "RUNNING"}),
            ("exit", "b", 0),
            ("event", "StepSucceeded", "Step 'b' completed successfully"),
            ("phases", {"a": "FAILED", "b": "SUCCEEDED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_on_failure_exit_code_gate_mismatches_skips_step(self, cluster):
        """Same as above, but a fails with a code outside the gate — b's
        condition can never be satisfied, so it's a dead end and SKIPPED."""
        dag = [
            {"name": "a", "depends_on": []},
            {
                "name": "b",
                "depends_on": [{"step": "a", "when": "ON_FAILURE", "on_exit_codes": [42]}],
            },
        ]
        cluster.script_step("a", exit_code=1, pods=[{"role": "worker", "exit_code": 7}])
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "SKIPPED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_failure_teardown_cancels_running_and_skips_pending_but_reactive_dependent_still_completes(self, cluster):
        """Fail-fast teardown: once `a` fails, the unrelated `b` (still
        RUNNING) is cancelled and the unrelated `c` (still PENDING, blocked
        on `b`) is skipped without ever running — but `e`, a's direct
        ON_FAILURE dependent, still gets submitted and runs to completion
        before the workflow finalizes FAILED."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": []},
            {"name": "c", "depends_on": ["b"]},
            {"name": "e", "depends_on": [{"step": "a", "when": "ON_FAILURE"}]},
        ]
        cluster.script_step("a", exit_code=1)
        cluster.script_step("b", never_completes=True)
        cluster.script_step("e", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("submit", "b"),
            ("phases", {"a": "RUNNING", "b": "RUNNING", "c": "PENDING", "e": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "RUNNING", "c": "SKIPPED", "e": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "e"),
            ("phases", {"a": "FAILED", "b": "RUNNING", "c": "SKIPPED", "e": "RUNNING"}),
            ("cancel", "b"),
            ("event", "StepCancelled", "Step 'b' was cancelled"),
            ("phases", {"a": "FAILED", "b": "CANCELED", "c": "SKIPPED", "e": "RUNNING"}),
            ("exit", "e", 0),
            ("event", "StepSucceeded", "Step 'e' completed successfully"),
            ("phases", {"a": "FAILED", "b": "CANCELED", "c": "SKIPPED", "e": "SUCCEEDED"}),
            # workflow finalizes FAILED even though e (a's ON_FAILURE dependent) itself succeeded.
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_failure_teardown_with_always_reactive_dependent(self, cluster):
        """Same DAG shape as the ON_FAILURE teardown test above, but e's
        reactive dependency on a is declared ALWAYS instead of ON_FAILURE —
        it must still run to completion before the workflow finalizes
        FAILED."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": []},
            {"name": "c", "depends_on": ["b"]},
            {"name": "e", "depends_on": [{"step": "a", "when": "ALWAYS"}]},
        ]
        cluster.script_step("a", exit_code=1)
        cluster.script_step("b", never_completes=True)
        cluster.script_step("e", exit_code=0)

        assert run_controller_main(cluster, dag) == 0

        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("submit", "b"),
            ("phases", {"a": "RUNNING", "b": "RUNNING", "c": "PENDING", "e": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "RUNNING", "c": "SKIPPED", "e": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "e"),
            ("phases", {"a": "FAILED", "b": "RUNNING", "c": "SKIPPED", "e": "RUNNING"}),
            ("cancel", "b"),
            ("event", "StepCancelled", "Step 'b' was cancelled"),
            ("phases", {"a": "FAILED", "b": "CANCELED", "c": "SKIPPED", "e": "RUNNING"}),
            ("exit", "e", 0),
            ("event", "StepSucceeded", "Step 'e' completed successfully"),
            ("phases", {"a": "FAILED", "b": "CANCELED", "c": "SKIPPED", "e": "SUCCEEDED"}),
            # workflow finalizes FAILED even though e (a's ALWAYS-reactive dependent) itself succeeded.
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_optional_step_failure_does_not_fail_workflow_or_teardown_others(self, cluster):
        """b is a conditional ALWAYS cleanup step marked `optional`: it fails,
        but that must not cascade into teardown of the unrelated, still-
        running `c`, and the workflow must still finalize SUCCEEDED."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}], "optional": True},
            {"name": "c", "depends_on": []},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("submit", "c"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "RUNNING"}),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "RUNNING"}),
            ("exit", "c", 0),
            ("event", "StepSucceeded", "Step 'c' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "SUCCEEDED"}),
            # status already reads SUCCEEDED here — b is optional and still
            # RUNNING, but the rollup excludes it entirely, not just its
            # eventual failure.
            ("status", "SUCCEEDED"),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "SUCCEEDED"}),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_optional_step_failure_still_gates_its_own_dependent(self, cluster):
        """Even though b is `optional`, its own FAILED phase still satisfies
        an ON_FAILURE dependent (d) exactly as any other step's failure
        would."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}], "optional": True},
            {"name": "d", "depends_on": [{"step": "b", "when": "ON_FAILURE"}]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("d", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "d": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "d": "PENDING"}),
            # b (optional) is excluded from the rollup entirely, so with a
            # done and d still PENDING on b, status reads PENDING rather
            # than RUNNING.
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "d": "PENDING"}),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "d": "PENDING"}),
            ("submit", "d"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "d": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "d", 0),
            ("event", "StepSucceeded", "Step 'd' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "d": "SUCCEEDED"}),
            ("status", "SUCCEEDED"),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_optional_step_failure_excluded_from_archived_status_json(self, cluster):
        """capture_status=True exercises the real status.py doc-build path —
        confirms the archived status.json's top-level "status" also excludes
        the optional step's FAILED phase, matching the live rollup."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}], "optional": True},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        assert run_controller_main(cluster, dag, capture_status=True) == 0
        assert cluster.status_doc["status"] == "SUCCEEDED"

    def test_optional_step_gets_torn_down_normally_when_unrelated_step_fails(self, cluster):
        """optional only protects the workflow from *that step's own* failure —
        it gives no immunity from teardown when a different, non-optional step
        fails elsewhere."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "x", "depends_on": [], "optional": True},
        ]
        cluster.script_step("a", exit_code=1)
        cluster.script_step("x", never_completes=True)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("submit", "x"),
            ("phases", {"a": "RUNNING", "x": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "x": "RUNNING"}),
            ("status", "FAILED"),
            ("cancel", "x"),
            ("event", "StepCancelled", "Step 'x' was cancelled"),
            ("phases", {"a": "FAILED", "x": "CANCELED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_optional_step_failure_does_not_shield_its_non_optional_dependent(self, cluster):
        """optional does not propagate: if a non-optional step depends on a
        failed optional step and itself fails, the workflow still fails on
        that dependent's own failure."""
        dag = [
            {"name": "a", "depends_on": []},
            {"name": "b", "depends_on": [{"step": "a", "when": "ALWAYS"}], "optional": True},
            {"name": "c", "depends_on": [{"step": "b", "when": "ON_FAILURE"}]},
        ]
        cluster.script_step("a", exit_code=0)
        cluster.script_step("b", exit_code=1)
        cluster.script_step("c", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING", "c": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 0),
            ("event", "StepSucceeded", "Step 'a' completed successfully"),
            ("phases", {"a": "SUCCEEDED", "b": "PENDING", "c": "PENDING"}),
            ("status", "PENDING"),
            ("submit", "b"),
            ("phases", {"a": "SUCCEEDED", "b": "RUNNING", "c": "PENDING"}),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "PENDING"}),
            ("submit", "c"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "RUNNING"}),
            ("status", "RUNNING"),
            ("exit", "c", 1),
            ("event", "StepFailed", "Step 'c' failed"),
            ("phases", {"a": "SUCCEEDED", "b": "FAILED", "c": "FAILED"}),
            ("status", "FAILED"),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['c']"),
        ]

    def test_optional_step_gated_by_exit_code_still_excluded_from_rollup_on_own_failure(self, cluster):
        """optional combines with on_exit_codes gating: b only runs because a's
        exit code matched the gate, and b's own subsequent failure is still
        excluded from the workflow rollup."""
        dag = [
            {"name": "a", "depends_on": []},
            {
                "name": "b",
                "depends_on": [{"step": "a", "when": "ON_FAILURE", "on_exit_codes": [42]}],
                "optional": True,
            },
        ]
        cluster.script_step("a", exit_code=1, pods=[{"role": "worker", "exit_code": 42}])
        cluster.script_step("b", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "PENDING"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING", "b": "PENDING"}),
            ("status", "RUNNING"),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED", "b": "PENDING"}),
            ("status", "FAILED"),
            ("submit", "b"),
            ("phases", {"a": "FAILED", "b": "RUNNING"}),
            ("exit", "b", 1),
            ("event", "StepFailed", "Step 'b' failed"),
            ("phases", {"a": "FAILED", "b": "FAILED"}),
            ("event", "WorkflowFailed", "Workflow failed — failed steps: ['a']"),
        ]

    def test_standalone_optional_step_failure_alone_does_not_fail_workflow(self, cluster):
        """A single optional step with no dependents and no dependencies:
        its failure alone must not fail the workflow."""
        dag = [
            {"name": "a", "depends_on": [], "optional": True},
        ]
        cluster.script_step("a", exit_code=1)
        assert run_controller_main(cluster, dag) == 0
        assert cluster.trace == [
            ("status", "UNKNOWN"),
            ("submit", "a"),
            ("phases", {"a": "RUNNING"}),
            ("exit", "a", 1),
            ("event", "StepFailed", "Step 'a' failed"),
            ("phases", {"a": "FAILED"}),
            ("event", "WorkflowSucceeded", "All steps completed successfully"),
        ]

    def test_exit_codes_of_a_gated_step_are_persisted_to_configmap(self, cluster):
        dag = [
            {"name": "a", "depends_on": []},
            {
                "name": "b",
                "depends_on": [{"step": "a", "when": "ON_FAILURE", "on_exit_codes": [42]}],
            },
        ]
        cluster.script_step("a", exit_code=1, pods=[{"role": "worker", "exit_code": 42}])
        cluster.script_step("b", exit_code=0)
        assert run_controller_main(cluster, dag) == 0
        cm_data = cluster.configmaps["wf-abc-phases"]["data"]
        assert json.loads(cm_data["exit_codes"]) == {"a": [42]}
