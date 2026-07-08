# Task: misc-fixes

**Status**: in-progress
**Branch**: hatchery/misc-fixes
**Created**: 2026-07-08 09:47

## Objective

Please address the following misc fixes:

 Backend / orchestration:
 - 1. Bare except Exception hides API/RBAC errors as empty state. [reported]
 workflow_state.py:476 (_list_jobsets_by_step) and :558 (is_jobset_suspended)
 swallow everything → chain status shows an empty tree on an RBAC/API error
 instead of surfacing it. Catch ApiException, re-raise non-404 or log.
 - 2. follow() can loop forever after the controller Job is GC'd. [reported]
 get_workflow_job_status (:539) has no 404 guard (unlike
 _read_workflow_metadata:447), so after ttlSecondsAfterFinished deletes the
 Job, status raises / degrades to UNKNOWN whose is_finished() is False.

 shell / cluster:
 - 3. Controller can stall the DAG on a transient submit error. [reported]
 _submit_ready_steps re-raises non-409; the step stays PENDING and is never
 retried (reconnect re-delivers only already-terminal events). Retry submit each
 watch iteration.
 - 4. Controller heartbeat can go stale on a long, event-quiet watch →
 liveness restart. [reported] w.stream() sets no timeout_seconds; heartbeat
 only advances on events. Use a periodic timeout/bookmark or a background touch.

 - 5 _build_failure_policy builds rules then discards them (only maxRestarts
 renders) — pre-existing, but per-rule actions are a silent no-op. [reported]

## Agreed Plan

1. **Fix 1 — Bare `except Exception` in `workflow_state.py`:**
   - `_list_jobsets_by_step` (line 476): catch `ApiException`, only swallow 404, re-raise others.
   - `is_jobset_suspended` (line 567): catch `ApiException`, only return False on 404, re-raise others.
   - Update tests: `test_unexpected_exception_returns_false` → `test_unexpected_exception_raises`; add propagation test for `_list_jobsets_by_step`.

2. **Fix 2 — `follow()` loops forever after controller Job GC'd:**
   - `get_workflow_job_status` (line 539): add 404 guard returning `(UNKNOWN, None)`, re-raise non-404.
   - `follow()` in `k8s_workflow.py`: treat `UNKNOWN` status as terminal (break loop).
   - Add test for 404 guard.

3. **Fix 3 — Controller stalls on transient submit error:**
   - `_submit_ready_steps`: catch non-409 `ApiException`, log, `continue` (leave step PENDING).
   - Add `_submit_ready_steps` call at top of each watch loop iteration for retry.
   - Update test `test_non_409_api_error_raises` → `test_non_409_api_error_stays_pending`. Add retry-on-next-iteration test.

4. **Fix 4 — Heartbeat stale on event-quiet watch:**
   - Add `timeout_seconds=30` to `w.stream()` call.
   - Add `_submit_ready_steps` call at top of watch loop (already in Fix 3) ensures heartbeat is touched each iteration.

5. **Fix 5 — `_build_failure_policy` discards rules:**
   - `_build_failure_policy` in `jobset.py`: include `rules` in returned dict.
   - `jobset.yaml.j2`: render `rules` list with `action` and `targetReplicatedJobs`.
   - Add manifest rendering test.

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
