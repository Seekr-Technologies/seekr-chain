# Task: delete-cancel

**Status**: complete
**Branch**: hatchery/delete-cancel
**Created**: 2026-04-21 09:26

## Objective

Add a `cancel` command - stop the workflow but do not delete it.

Also: both `cancel` and `delete` should print confirmation, and both should accept a list of args.

## Context

The `chain delete` CLI command existed but only accepted a single `JOB_ID`, printed no confirmation, and there was no way to stop a workflow without deleting it entirely from the cluster.

Argo Workflows supports stopping a workflow (preserving its history/logs) by patching `spec.shutdown: Terminate` on the workflow object, which transitions it to `TERMINATED` status (already defined in `WorkflowStatus`). This is distinct from deletion, which removes the CRD entirely.

## Summary

### Key decisions

- **Cancel mechanism**: Uses `patch_namespaced_custom_object()` with `{"spec": {"shutdown": "Terminate"}}` — the standard Argo way to stop without destroying. The `TERMINATED` status was already in the `WorkflowStatus` enum and treated as a failure state, so no enum changes were needed.
- **Variadic args**: Click's `nargs=-1, required=True` on a positional argument gives a tuple of values, enabling `chain delete job-1 job-2` syntax. The loop iterates and prints confirmation per workflow.
- **Confirmation format**: `"Deleted: {job_id}"` / `"Cancelled: {job_id}"` — simple, one line per workflow.

### Files changed

| File | Change |
|------|--------|
| `src/seekr_chain/workflow.py` | Added abstract `cancel()` method |
| `src/seekr_chain/backends/argo/argo_workflow.py` | Added `cancel()` using `patch_namespaced_custom_object` |
| `src/seekr_chain/cli.py` | Updated `delete` to variadic args + confirmation; added `cancel` command |
| `tests/unit/test_cli.py` | Updated `TestDelete` (single + multi); added `TestCancel` (single + multi) |

### Gotchas for future agents

- The repo's `.venv` has macOS symlinks (built on a Mac) and is unusable in Docker. Use `PYTHONPATH=/repo/.hatchery/worktrees/local-execution/.venv/lib/python3.13/site-packages:src /usr/bin/python3 -m pytest --noconftest` to run unit tests. The `--noconftest` skips the root `tests/conftest.py` which imports boto3/kubernetes and tries to spin up a k3d cluster.
- Integration test for cancel would verify the workflow transitions to `TERMINATED` status after patching (not tested here as that requires a live cluster).
