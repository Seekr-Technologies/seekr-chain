#!/usr/bin/env python3
"""Local execution backend for seekr-chain.

Runs workflow steps directly in the local environment — no cluster, no S3,
no Docker. Execution is synchronous, steps run in DAG order, and output
streams to the terminal.

Limitations:
- Multi-node steps (num_nodes > 1) are coerced to 1 with a warning.
- Multi-role steps (MultiRoleStepConfig) are not supported.
"""

import json
import logging
import os
import socket
import subprocess
import tempfile

from seekr_chain.config import MultiRoleStepConfig, SingleRoleStepConfig, WorkflowConfig
from seekr_chain.dag import topological_sort
from seekr_chain.status import WorkflowStatus
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


class LocalWorkflow(Workflow):
    """Represents a completed (or failed) local workflow execution."""

    def __init__(self, name: str, succeeded: bool):
        self._name = name
        self._succeeded = succeeded

    @property
    def id(self) -> str:
        return self._name

    @property
    def name(self) -> str:
        return self._name

    def get_status(self) -> WorkflowStatus:
        return WorkflowStatus.SUCCEEDED if self._succeeded else WorkflowStatus.FAILED

    def get_detailed_state(self):
        return None

    def follow(self, **kwargs):
        pass  # Execution already complete; output was streamed live.

    def attach(self):
        raise NotImplementedError("Local mode does not support attach")

    def delete(self):
        pass  # Nothing to clean up for local execution.

    def cancel(self):
        pass  # Nothing to cancel for local execution.

    def get_logs(self, **kwargs):
        pass  # Logs were streamed to stdout during execution.


def _run_script(shell: str, script_content: str, cwd: str, env: dict, step_name: str, phase: str) -> int:
    """Write script_content to a temp file and run it. Returns exit code."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script_content)
        script_path = f.name

    try:
        logger.debug(f"[{step_name}] Running {phase}")
        result = subprocess.run([shell, script_path], cwd=cwd, env=env)
        return result.returncode
    finally:
        os.unlink(script_path)


def _run_step(step: SingleRoleStepConfig, workdir: str, env: dict) -> bool:
    """Execute a single step. Returns True if the main script succeeded."""
    logger.info(f"--- Step: {step.name} ---")

    before_rc = 0
    if step.before_script:
        before_rc = _run_script(step.shell, step.before_script, workdir, env, step.name, "before_script")

    main_rc = 1
    if before_rc == 0:
        main_rc = _run_script(step.shell, step.script, workdir, env, step.name, "script")
    else:
        logger.warning(f"[{step.name}] before_script failed (exit {before_rc}), skipping main script")

    if step.after_script:
        _run_script(step.shell, step.after_script, workdir, env, step.name, "after_script")

    return main_rc == 0


def launch_local_workflow(
    config: dict | WorkflowConfig,
    *,
    interactive: bool = False,
    attach: bool = True,
    args: dict | None = None,
) -> LocalWorkflow:
    """Execute a workflow locally. Returns a LocalWorkflow object.

    Parameters
    ----------
    config
        Workflow configuration (dict or WorkflowConfig).
    interactive
        Accepted for API compatibility; ignored in local mode.
    attach
        Accepted for API compatibility; ignored in local mode.
    args
        Workflow args dict. Written to a temp file and exposed via
        SEEKR_CHAIN_ARGS, mirroring the Argo backend behaviour.
    """
    if isinstance(config, dict):
        config = WorkflowConfig.model_validate(config)

    # Validate supported step types; warn and coerce where possible.
    # Collect num_nodes overrides without mutating the caller's config.
    num_nodes_override: dict[str, int] = {}
    for step in config.steps:
        if isinstance(step, MultiRoleStepConfig):
            raise ValueError(
                f"Local mode does not support multi-role steps (step: '{step.name}'). "
                "Use the Argo backend for multi-role steps."
            )
        if step.resources.num_nodes > 1:
            logger.warning(
                f"Step '{step.name}' requests num_nodes={step.resources.num_nodes}; "
                "local mode runs as a single node (num_nodes=1)."
            )
            num_nodes_override[step.name] = 1

    ordered_steps = topological_sort(config.steps)

    workdir = config.code.path if config.code else os.getcwd()

    # Write args to a temp file so SEEKR_CHAIN_ARGS points to real JSON,
    # matching what the Argo backend provides inside containers.
    args_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        json.dump(args or {}, args_file)
        args_file.flush()
        args_path = args_file.name
    finally:
        args_file.close()

    workflow_id = config.name

    base_env = {
        **os.environ,
        **(config.env or {}),
        # Distributed training vars
        "NNODES": "1",
        "NODE_RANK": "0",
        "MASTER_ADDR": "localhost",
        "MASTER_PORT": "29500",
        "RESTART_ATTEMPT": "0",
        "NODE_NAME": socket.gethostname(),
        # seekr-chain identity vars
        "SEEKR_CHAIN_WORKFLOW_ID": workflow_id,
        "SEEKR_CHAIN_ARGS": args_path,
    }

    # Track which steps failed so dependents can be skipped.
    failed_steps: set[str] = set()
    workflow_succeeded = True

    try:
        for step in ordered_steps:
            # Skip steps whose dependencies failed.
            blocked_by = {dep for dep in (step.depends_on or []) if dep in failed_steps}
            if blocked_by:
                logger.warning(f"Skipping step '{step.name}' because dependencies failed: {blocked_by}")
                failed_steps.add(step.name)
                continue

            num_nodes = num_nodes_override.get(step.name, step.resources.num_nodes)
            pod_id = f"{workflow_id}-{step.name}-0"

            step_env = {
                **base_env,
                "GPUS_PER_NODE": str(step.resources.gpus_per_node),
                # Per-step identity vars
                "SEEKR_CHAIN_JOBSET_ID": step.name,
                "SEEKR_CHAIN_POD_ID": pod_id,
                "SEEKR_CHAIN_POD_INSTANCE_ID": f"{pod_id}-0",
                # Override NNODES in case this step was coerced
                "NNODES": str(num_nodes),
                **(step.env or {}),
            }

            if not _run_step(step, workdir, step_env):
                logger.error(f"Step '{step.name}' failed")
                failed_steps.add(step.name)
                workflow_succeeded = False
    finally:
        os.unlink(args_path)

    return LocalWorkflow(name=config.name, succeeded=workflow_succeeded)
