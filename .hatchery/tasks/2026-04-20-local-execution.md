# Task: local-execution

**Status**: complete
**Branch**: hatchery/local-execution
**Created**: 2026-04-20 13:45

## Objective

Enable local execution to facilitate debugging. When running in local mode, execute steps in DAG order directly in the local environment. Multi-node and multi-role steps are not supported in this mode.

## Context

seekr-chain previously only supported the Argo backend, requiring a running Kubernetes cluster, S3 datastore, and credentials — making local debugging difficult. The codebase already had a `Backend` enum and an abstract `Workflow` base class designed to accommodate additional backends.

## Summary

### What was done

Added a `LOCAL` backend with the following shape:

- **`Backend.LOCAL`** added to `src/seekr_chain/workflow.py`
- **`src/seekr_chain/dag.py`** *(new)* — shared `topological_sort` (Kahn's algorithm) extracted here so any future sequential backend (Slurm, Docker Compose, etc.) can reuse it without depending on `local_workflow`
- **`src/seekr_chain/backends/local/local_workflow.py`** — core implementation:
  - `LocalWorkflow` implements the `Workflow` ABC; `attach()` raises `NotImplementedError`, all other lifecycle methods (`follow`, `delete`, `get_logs`) are no-ops since execution is synchronous and output is already streamed
  - `launch_local_workflow()` validates step types, topologically sorts the DAG, and runs each step sequentially via `subprocess.run`
  - Multi-node steps (`num_nodes > 1`) emit a warning and are coerced to 1 internally — the caller's `config` object is **not mutated** (override tracked in a local dict)
  - `_run_step()` mirrors `chain-entrypoint.sh`: `after_script` always runs; `script` is skipped if `before_script` fails
  - Subprocess calls inherit the terminal (no stdout/stderr capture), so output streams live
  - Full Argo env-var parity: `NNODES`, `NODE_RANK`, `MASTER_ADDR`, `MASTER_PORT`, `RESTART_ATTEMPT`, `NODE_NAME`, `SEEKR_CHAIN_WORKFLOW_ID`, `SEEKR_CHAIN_ARGS` (real JSON file), `SEEKR_CHAIN_JOBSET_ID`, `SEEKR_CHAIN_POD_ID`, `SEEKR_CHAIN_POD_INSTANCE_ID`, `GPUS_PER_NODE`
- **`src/seekr_chain/__init__.py`** — imports `LocalWorkflow`/`launch_local_workflow`, dispatches on `Backend.LOCAL`, and catches `ValueError` from `Backend()` conversion to produce a clean "Unknown backend X. Valid backends: [...]" message
- **`src/seekr_chain/cli.py`** — adds `-b/--backend` flag to `chain submit` using `click.Choice([b.value.lower() for b in Backend])` so valid values are derived from the enum (free validation + autocomplete)
- **`tests/unit/test_local_execution.py`** — 23 unit tests covering `LocalWorkflow` interface, topological sort, validation errors, DAG execution order, failure propagation, `after_script` always-runs, env var injection, and CLI wiring

### Key decisions

- **Synchronous execution**: local mode runs steps inline (no threads/async), so `launch_local_workflow` blocks until the workflow finishes. Right tradeoff for a debugging tool.
- **Multi-node warning not error**: coercing `num_nodes > 1` to 1 lets existing multi-node configs (e.g. `examples/4_dag/`) run locally without modification.
- **Unsupported step types raise `ValueError` eagerly**: `MultiRoleStepConfig` raises before any step runs, so users get a clear error rather than a partial execution failure.
- **Working directory**: defaults to `os.getcwd()` when `config.code` is absent; uses `config.code.path` otherwise. The `image` field is ignored in local mode.
- **Args file written to disk**: `args` dict is serialized to a real temp JSON file so `SEEKR_CHAIN_ARGS` points to valid JSON, matching Argo's container-side behavior exactly. Cleaned up in `finally`.

### Patterns established

- New backends live in `src/seekr_chain/backends/<name>/` with an `__init__.py` exporting `<Name>Workflow` and `launch_<name>_workflow`.
- DAG ordering lives in `src/seekr_chain/dag.py`; import `topological_sort` from there, not from a backend module.
- Dispatch is in `src/seekr_chain/__init__.py` inside `launch_workflow()`; the `Backend` enum is the registry.
- CLI `--backend` option uses `click.Choice([b.value.lower() for b in Backend])` so adding a new `Backend` value automatically exposes it in the CLI.
- Unit tests for the backend go in `tests/unit/test_<name>_execution.py` and must be run with `--real-cluster` (to bypass the autouse `patch_configs_for_testing` fixture which tries to provision a k3d cluster in hermetic mode).

### Gotchas

- The root `tests/conftest.py` has an `autouse=True` fixture `patch_configs_for_testing` that depends on `k3d_cluster`, which fails if `docker`/`k3d`/`kubectl`/`argo` are not installed. Pass `--real-cluster` when running unit tests in an environment without those tools. This is a pre-existing issue, not introduced here.
- The `.venv` in the worktree is macOS-linked (created on a Mac). On Linux, run tests via `/home/hatchery/.local/bin/uv run pytest` after installing `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh && uv sync`).
