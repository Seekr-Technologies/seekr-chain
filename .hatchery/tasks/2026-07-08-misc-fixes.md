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

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
