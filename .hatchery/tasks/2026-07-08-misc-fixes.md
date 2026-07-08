# Task: misc-fixes

**Status**: complete
**Branch**: hatchery/misc-fixes
**Created**: 2026-07-08 09:47

## Objective

Address five reported misc fixes across backend/orchestration and shell/cluster:

1. Bare `except Exception` hides API/RBAC errors as empty state in `workflow_state.py`.
2. `follow()` can loop forever after the controller Job is GC'd by `ttlSecondsAfterFinished`.
3. Controller can stall the DAG on a transient submit error.
4. Controller heartbeat can go stale on a long, event-quiet watch → liveness restart.
5. `_build_failure_policy` builds rules then discards them (only `maxRestarts` renders).

## Summary

### Fix 1 — Surface API/RBAC errors instead of swallowing as empty state

**Files:** `src/seekr_chain/backends/k8s/workflow_state.py`

- `_list_jobsets_by_step` (line 476): Changed bare `except Exception` to `except ApiException`. Only 404 is swallowed (returns empty dict); all other API errors (RBAC 403, 500, etc.) propagate so the caller sees them instead of an empty tree.
- `is_jobset_suspended` (line 558): Same pattern — only 404 returns `False` (JobSet deleted = not suspended); other errors re-raise.

**Decision:** A 404 on a list call means the resource doesn't exist, which is a valid empty state. Everything else is an error the user should see. The previous `except Exception` caught everything including `RuntimeError`, hiding programming errors too.

### Fix 2 — `follow()` loops forever after controller Job GC'd

**Files:** `src/seekr_chain/backends/k8s/workflow_state.py`, `src/seekr_chain/backends/k8s/k8s_workflow.py`

- `get_workflow_job_status` (line 539): Added a 404 guard matching `_read_workflow_metadata` (line 447). Returns `(WorkflowStatus.UNKNOWN, None)` on 404; re-raises non-404.
- `follow()` in `k8s_workflow.py`: Added `workflow_state.status == WorkflowStatus.UNKNOWN` as a terminal condition to break the loop. The only way status goes UNKNOWN is the controller Job being deleted (404), so this is safe.

### Fix 3 — Controller stalls on transient submit error

**Files:** `src/seekr_chain/backends/k8s/resources/controller.py`

- `_submit_ready_steps`: Non-409 `ApiException` is now caught, logged, and the step is left PENDING via `continue` (instead of re-raising). The step is retried on the next watch iteration.
- Added `_submit_ready_steps` call at the top of each watch loop iteration (before `w.stream()`), so steps that failed to submit previously get retried even without new events arriving. This is essential because the watch only re-delivers already-terminal events — a step stuck at PENDING would never be retried otherwise.

**Decision:** Re-raising caused the submit error to propagate up to the `except ApiException` handler in the watch loop, which would sleep and reconnect the watch. But the watch reconnect only re-delivers terminal events for already-submitted JobSets — a step that was never successfully submitted has no JobSet to watch, so it stays PENDING forever.

### Fix 4 — Heartbeat stale on event-quiet watch

**Files:** `src/seekr_chain/backends/k8s/resources/controller.py`

- Added `_WATCH_TIMEOUT_SECONDS = 30` constant.
- Added `timeout_seconds=_WATCH_TIMEOUT_SECONDS` to `w.stream()`. The Kubernetes watch client returns normally when the timeout fires (no exception), so the outer `while` loop iterates and hits `_touch_heartbeat()` at the top.
- Combined with the `_submit_ready_steps` call at the top of each iteration (from Fix 3), the heartbeat is now touched at least every 30 seconds even when no events arrive.

### Fix 5 — Render failure_policy rules in JobSet manifests

**Files:** `src/seekr_chain/backends/k8s/jobset.py`, `src/seekr_chain/backends/k8s/templates/jobset.yaml.j2`

- `_build_failure_policy`: Now includes `rules` in the returned policy dict when rules are present (previously built the list but discarded it).
- `jobset.yaml.j2`: Template now renders `rules:` with `action` and `targetReplicatedJobs` fields. Uses `failure_policy.get("rules")` to safely handle policies without rules (Jinja2 raises `UndefinedError` on missing dict keys with attribute syntax).

**Gotcha:** Jinja2's `{% if failure_policy.rules %}` raises `UndefinedError` when `rules` is not a key in the dict. Must use `{% if failure_policy.get("rules") %}` instead.

### Tests

**Files:** `tests/unit/backends/k8s/test_collect_states.py`, `tests/unit/test_controller.py`, `tests/unit/test_manifest_rendering.py`

- Updated `test_unexpected_exception_returns_false` → `test_non_404_api_exception_raises` and `test_unexpected_exception_raises` for `is_jobset_suspended`.
- Added `TestListJobsetsByStep` class with tests for normal operation, 404, and non-404 propagation.
- Added `TestGetWorkflowJobStatus` class with tests for 404 guard, non-404 propagation, and running job.
- Changed `test_non_409_api_error_raises` → `test_non_409_api_error_stays_pending`.
- Added `TestWatchTimeout` and `TestTransientSubmitRetry` test classes.
- Added failure_policy rules rendering tests (single-role, multi-role with target_roles, no-rules default).

All 467 unit tests pass.
