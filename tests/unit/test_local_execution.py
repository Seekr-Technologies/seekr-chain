"""Unit tests for the LOCAL execution backend."""

import textwrap
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from seekr_chain.backends.local.local_workflow import (
    LocalWorkflow,
    launch_local_workflow,
)
from seekr_chain.cli import main
from seekr_chain.config import MultiRoleStepConfig, RoleSpecConfig, WorkflowConfig
from seekr_chain.dag import topological_sort as _topological_sort
from seekr_chain.status import WorkflowStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(steps_yaml: str) -> WorkflowConfig:
    import yaml

    raw = yaml.safe_load(
        textwrap.dedent(f"""\
        name: test-workflow
        steps:
        {steps_yaml}
    """)
    )
    return WorkflowConfig.model_validate(raw)


SINGLE_STEP_CONFIG = textwrap.dedent("""\
    name: test-workflow
    steps:
      - name: step
        image: ubuntu:24.04
        script: echo hello
""")


# ---------------------------------------------------------------------------
# LocalWorkflow class
# ---------------------------------------------------------------------------


class TestLocalWorkflow:
    def test_succeeded_status(self):
        wf = LocalWorkflow(name="my-wf", succeeded=True)
        assert wf.get_status() == WorkflowStatus.SUCCEEDED

    def test_failed_status(self):
        wf = LocalWorkflow(name="my-wf", succeeded=False)
        assert wf.get_status() == WorkflowStatus.FAILED

    def test_id_and_name(self):
        wf = LocalWorkflow(name="my-wf", succeeded=True)
        assert wf.id == "my-wf"
        assert wf.name == "my-wf"

    def test_follow_is_noop(self):
        wf = LocalWorkflow(name="x", succeeded=True)
        wf.follow()  # should not raise

    def test_delete_is_noop(self):
        wf = LocalWorkflow(name="x", succeeded=True)
        wf.delete()  # should not raise

    def test_get_logs_is_noop(self):
        wf = LocalWorkflow(name="x", succeeded=True)
        wf.get_logs()  # should not raise

    def test_attach_raises(self):
        wf = LocalWorkflow(name="x", succeeded=True)
        with pytest.raises(NotImplementedError):
            wf.attach()

    def test_get_detailed_state_is_none(self):
        wf = LocalWorkflow(name="x", succeeded=True)
        assert wf.get_detailed_state() is None


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_single_step(self):
        config = WorkflowConfig.model_validate(
            {"name": "t", "steps": [{"name": "a", "image": "ubuntu:24.04", "script": "echo a"}]}
        )
        ordered = _topological_sort(config.steps)
        assert [s.name for s in ordered] == ["a"]

    def test_linear_chain(self):
        config = WorkflowConfig.model_validate(
            {
                "name": "t",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "echo a"},
                    {"name": "b", "image": "ubuntu:24.04", "script": "echo b", "depends_on": ["a"]},
                    {"name": "c", "image": "ubuntu:24.04", "script": "echo c", "depends_on": ["b"]},
                ],
            }
        )
        ordered = _topological_sort(config.steps)
        names = [s.name for s in ordered]
        assert names.index("a") < names.index("b")
        assert names.index("b") < names.index("c")

    def test_diamond_dag(self):
        """a → b, a → c, b+c → d."""
        config = WorkflowConfig.model_validate(
            {
                "name": "t",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "echo a"},
                    {"name": "b", "image": "ubuntu:24.04", "script": "echo b", "depends_on": ["a"]},
                    {"name": "c", "image": "ubuntu:24.04", "script": "echo c", "depends_on": ["a"]},
                    {"name": "d", "image": "ubuntu:24.04", "script": "echo d", "depends_on": ["b", "c"]},
                ],
            }
        )
        ordered = _topological_sort(config.steps)
        names = [s.name for s in ordered]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("d")
        assert names.index("c") < names.index("d")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_multirole_step_raises(self):
        config = WorkflowConfig(
            name="test",
            steps=[
                MultiRoleStepConfig(
                    name="multi",
                    roles=[
                        RoleSpecConfig(name="role1", image="ubuntu:24.04", script="echo 1"),
                        RoleSpecConfig(name="role2", image="ubuntu:24.04", script="echo 2"),
                    ],
                )
            ],
        )
        with pytest.raises(ValueError, match="multi-role"):
            launch_local_workflow(config)

    def test_multinode_step_warns_and_runs(self):
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [{"name": "step", "image": "ubuntu:24.04", "script": "exit 0", "resources": {"num_nodes": 2}}],
            }
        )
        # Should not raise — multi-node is coerced to 1 with a warning.
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.SUCCEEDED
        # config is not mutated; the override happens internally.
        assert config.steps[0].resources.num_nodes == 2


# ---------------------------------------------------------------------------
# Step execution (uses real subprocesses with simple shell commands)
# ---------------------------------------------------------------------------


class TestStepExecution:
    def test_single_step_success(self, tmp_path):
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [{"name": "s", "image": "ubuntu:24.04", "script": "exit 0"}],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.SUCCEEDED

    def test_single_step_failure(self):
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [{"name": "s", "image": "ubuntu:24.04", "script": "exit 1"}],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.FAILED

    def test_dag_order(self, tmp_path):
        """Steps with depends_on execute in the right order."""
        order_file = tmp_path / "order.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        "name": "first",
                        "image": "ubuntu:24.04",
                        "script": f"echo first >> {order_file}",
                    },
                    {
                        "name": "second",
                        "image": "ubuntu:24.04",
                        "script": f"echo second >> {order_file}",
                        "depends_on": ["first"],
                    },
                ],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.SUCCEEDED
        lines = order_file.read_text().splitlines()
        assert lines == ["first", "second"]

    def test_failed_step_skips_dependents(self, tmp_path):
        """If step A fails, step B (depends on A) must not run."""
        marker = tmp_path / "ran.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "exit 1"},
                    {
                        "name": "b",
                        "image": "ubuntu:24.04",
                        "script": f"touch {marker}",
                        "depends_on": ["a"],
                    },
                ],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.FAILED
        assert not marker.exists(), "dependent step should not have run"

    def test_after_script_always_runs(self, tmp_path):
        """after_script runs even when the main script fails."""
        marker = tmp_path / "after.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "script": "exit 1",
                        "after_script": f"touch {marker}",
                    }
                ],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.FAILED
        assert marker.exists(), "after_script should have created the marker file"

    def test_env_vars_injected(self, tmp_path):
        """Global and step env vars are visible to the script."""
        out = tmp_path / "env.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "env": {"GLOBAL_VAR": "global"},
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "script": f"echo $GLOBAL_VAR $STEP_VAR > {out}",
                        "env": {"STEP_VAR": "step"},
                    }
                ],
            }
        )
        launch_local_workflow(config)
        assert out.read_text().strip() == "global step"

    def test_nnodes_and_node_rank_injected(self, tmp_path):
        """Standard local-mode env vars NNODES and NODE_RANK are always set."""
        out = tmp_path / "vars.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "script": f"echo $NNODES $NODE_RANK > {out}",
                    }
                ],
            }
        )
        launch_local_workflow(config)
        assert out.read_text().strip() == "1 0"


# ---------------------------------------------------------------------------
# CLI --backend flag
# ---------------------------------------------------------------------------


class TestCliBackendFlag:
    def _config_file(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text(SINGLE_STEP_CONFIG)
        return p

    def test_default_backend_is_argo(self, tmp_path):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", str(self._config_file(tmp_path))])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "argo"

    def test_local_backend_flag(self, tmp_path):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "--backend", "local", str(self._config_file(tmp_path))])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "local"

    def test_short_backend_flag(self, tmp_path):
        mock_job = MagicMock()
        runner = CliRunner()

        with patch("seekr_chain.launch_workflow", return_value=mock_job) as mock_launch:
            result = runner.invoke(main, ["submit", "-b", "local", str(self._config_file(tmp_path))])

        assert result.exit_code == 0, result.output
        assert mock_launch.call_args.kwargs.get("backend") == "local"
