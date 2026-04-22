"""Unit tests for the LOCAL execution backend."""

import json
import socket

import pytest

from seekr_chain.backends.local.local_workflow import (
    LocalWorkflow,
    launch_local_workflow,
)
from seekr_chain.config import MultiRoleStepConfig, RoleSpecConfig, WorkflowConfig
from seekr_chain.status import WorkflowStatus

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

    def test_all_injected_env_vars(self, tmp_path):
        """Every env var set by local mode is present with the expected value."""
        out = tmp_path / "vars.txt"
        script = f"""
{{
printf 'NNODES=%s\\n' "$NNODES"
printf 'NODE_RANK=%s\\n' "$NODE_RANK"
printf 'MASTER_ADDR=%s\\n' "$MASTER_ADDR"
printf 'MASTER_PORT=%s\\n' "$MASTER_PORT"
printf 'RESTART_ATTEMPT=%s\\n' "$RESTART_ATTEMPT"
printf 'NODE_NAME=%s\\n' "$NODE_NAME"
printf 'GPUS_PER_NODE=%s\\n' "$GPUS_PER_NODE"
printf 'SEEKR_CHAIN_WORKFLOW_ID=%s\\n' "$SEEKR_CHAIN_WORKFLOW_ID"
printf 'SEEKR_CHAIN_JOBSET_ID=%s\\n' "$SEEKR_CHAIN_JOBSET_ID"
printf 'SEEKR_CHAIN_POD_ID=%s\\n' "$SEEKR_CHAIN_POD_ID"
printf 'SEEKR_CHAIN_POD_INSTANCE_ID=%s\\n' "$SEEKR_CHAIN_POD_INSTANCE_ID"
printf 'SEEKR_CHAIN_ARGS=%s\\n' "$SEEKR_CHAIN_ARGS"
}} > {out}
"""
        config = WorkflowConfig.model_validate(
            {
                "name": "my-workflow",
                "steps": [{"name": "my-step", "image": "ubuntu:24.04", "script": script}],
            }
        )
        launch_local_workflow(config)

        env = dict(line.split("=", 1) for line in out.read_text().splitlines())

        assert env["NNODES"] == "1"
        assert env["NODE_RANK"] == "0"
        assert env["MASTER_ADDR"] == "localhost"
        assert env["MASTER_PORT"] == "29500"
        assert env["RESTART_ATTEMPT"] == "0"
        assert env["NODE_NAME"] == socket.gethostname()
        assert env["GPUS_PER_NODE"] == "0"
        assert env["SEEKR_CHAIN_WORKFLOW_ID"] == "my-workflow"
        assert env["SEEKR_CHAIN_JOBSET_ID"] == "my-step"
        assert env["SEEKR_CHAIN_POD_ID"] == "my-workflow-my-step-0"
        assert env["SEEKR_CHAIN_POD_INSTANCE_ID"] == "my-workflow-my-step-0-0"
        assert env["SEEKR_CHAIN_ARGS"] != ""

    def test_args_written_to_seekr_chain_args(self, tmp_path):
        """SEEKR_CHAIN_ARGS points to a valid JSON file containing the passed args."""
        out = tmp_path / "args.json"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "script": f"cp $SEEKR_CHAIN_ARGS {out}",
                    }
                ],
            }
        )
        launch_local_workflow(config, args={"lr": 0.01, "epochs": 5})
        data = json.loads(out.read_text())
        assert data == {"lr": 0.01, "epochs": 5}

    def test_before_script_failure_skips_script(self, tmp_path):
        """If before_script fails, the main script must not run and the step fails."""
        marker = tmp_path / "ran.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "before_script": "exit 1",
                        "script": f"touch {marker}",
                    }
                ],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.FAILED
        assert not marker.exists(), "script should not have run after before_script failed"

    def test_independent_steps_both_run_when_one_fails(self, tmp_path):
        """Two independent steps: if A fails, B (no dependency on A) still runs."""
        marker = tmp_path / "b_ran.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "steps": [
                    {"name": "a", "image": "ubuntu:24.04", "script": "exit 1"},
                    {"name": "b", "image": "ubuntu:24.04", "script": f"touch {marker}"},
                ],
            }
        )
        wf = launch_local_workflow(config)
        assert wf.get_status() == WorkflowStatus.FAILED
        assert marker.exists(), "independent step B should have run despite A failing"

    def test_config_as_dict(self):
        """launch_local_workflow accepts a raw dict and validates it internally."""
        config_dict = {
            "name": "test",
            "steps": [{"name": "s", "image": "ubuntu:24.04", "script": "exit 0"}],
        }
        wf = launch_local_workflow(config_dict)
        assert wf.get_status() == WorkflowStatus.SUCCEEDED

    def test_code_path_sets_workdir(self, tmp_path):
        """config.code.path is used as the working directory for script execution."""
        out = tmp_path / "cwd.txt"
        config = WorkflowConfig.model_validate(
            {
                "name": "test",
                "code": {"path": str(tmp_path)},
                "steps": [
                    {
                        "name": "s",
                        "image": "ubuntu:24.04",
                        "script": f"pwd > {out}",
                    }
                ],
            }
        )
        launch_local_workflow(config)
        assert out.read_text().strip() == str(tmp_path)
