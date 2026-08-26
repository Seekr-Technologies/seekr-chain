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

from seekr_chain.config import DependsOnCondition, MultiRoleStepConfig, SingleRoleStepConfig, WorkflowConfig
from seekr_chain.dag import topological_sort
from seekr_chain.status_model import Status
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


# Phases meaning a dependency did not succeed — a valid ON_FAILURE/ALWAYS
# trigger regardless of *why* it didn't succeed. Mirrors the k8s controller's
# `_NOT_SUCCEEDED_PHASES` (see resources/controller/phases.py) minus CANCELLED
# — local mode has no external cancellation.
_NOT_SUCCEEDED_PHASES = ("FAILED", "SKIPPED")


def _dep_satisfied(phase: str, cond: DependsOnCondition, exit_codes: dict[str, int]) -> bool:
    """True if `cond` is satisfied given the dependency's terminal phase.

    Mirrors the k8s controller's `dep_satisfied` (see
    resources/controller/phases.py).
    """
    if cond.when == "ALWAYS":
        return True
    if cond.when == "ON_SUCCESS":
        return phase == "SUCCEEDED"
    # ON_FAILURE
    if phase not in _NOT_SUCCEEDED_PHASES:
        return False
    if cond.on_exit_codes is None:
        return True
    matched = exit_codes.get(cond.step) in cond.on_exit_codes
    return matched if cond.operator == "IN" else not matched


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

    def get_status(self) -> Status:
        return Status.SUCCEEDED if self._succeeded else Status.FAILED

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


def _run_step(step: SingleRoleStepConfig, workdir: str, env: dict) -> int:
    """Execute a single step. Returns the main script's exit code."""
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

    return main_rc


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

    # Terminal phase ("SUCCEEDED"/"FAILED"/"SKIPPED") of each step run so far,
    # and the main script's exit code (for depends_on.on_exit_codes gating).
    step_phase: dict[str, str] = {}
    exit_codes: dict[str, int] = {}
    workflow_succeeded = True
    # Becomes True the moment any step FAILS. From then on, a step only still
    # runs if it's a direct ON_FAILURE/ALWAYS dependent of a FAILED step
    # (reactive teardown) — everything else, including independent branches
    # that would otherwise be ready, is skipped without running. A failed
    # step always fails the workflow, no exceptions.
    teardown = False

    try:
        for step in ordered_steps:
            deps = step.depends_on

            if teardown:
                reactive = any(
                    cond.when in ("ON_FAILURE", "ALWAYS")
                    and step_phase.get(cond.step) == "FAILED"
                    and _dep_satisfied(step_phase[cond.step], cond, exit_codes)
                    for cond in deps
                )
                if not reactive:
                    logger.warning(f"Skipping step '{step.name}': workflow already failed")
                    step_phase[step.name] = "SKIPPED"
                    continue
            elif not all(_dep_satisfied(step_phase[cond.step], cond, exit_codes) for cond in deps):
                # A dead end: this step's trigger condition (e.g. an
                # ON_FAILURE edge whose target succeeded) can never fire —
                # not itself a failure. The reactive-only dead-end validator
                # (config.py's check_depends_on) guarantees no other step
                # depends on it, so SKIPPED never needs to propagate further.
                logger.warning(f"Skipping step '{step.name}': depends_on conditions not met")
                step_phase[step.name] = "SKIPPED"
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

            rc = _run_step(step, workdir, step_env)
            exit_codes[step.name] = rc
            if rc == 0:
                step_phase[step.name] = "SUCCEEDED"
            else:
                logger.error(f"Step '{step.name}' failed")
                step_phase[step.name] = "FAILED"
                workflow_succeeded = False
                teardown = True
    finally:
        os.unlink(args_path)

    return LocalWorkflow(name=config.name, succeeded=workflow_succeeded)
