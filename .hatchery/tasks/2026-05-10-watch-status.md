# Task: watch-status

**Status**: complete
**Branch**: hatchery/watch-status
**Created**: 2026-05-10 09:16

## Objective

Replace fixed-interval polling in `wait()` and `follow()` with event-driven status watching, eliminating polling latency. Add a `watch_status()` method to the backend interface with an optimized Kubernetes Watch API implementation.

## Context

`wait()` polled `get_status()` every 10 s; `follow()` polled every 1 s. The in-cluster `controller.py` already used `kubernetes.watch.Watch` for zero-latency event detection. The goal was to expose the same capability to the client side, non-blocking for multiple concurrent jobs.

## Summary

### Files changed

- **`src/seekr_chain/workflow.py`** — added `watch_status()` as a non-abstract method with a 2 s polling fallback. The fallback deduplicates yielding (only yields on change) and stops once the status is finished. `poll_interval` was intentionally kept out of the signature — it's meaningless for event-driven overrides and would leak a polling detail into the public API.

- **`src/seekr_chain/backends/k8s/k8s_workflow.py`** — `K8sWorkflow.watch_status()` uses `kubernetes.watch.Watch` on `list_namespaced_job` scoped by `field_selector=metadata.name=<id>`. Mirrors the `resourceVersion`-resumption and 410-Gone relist pattern from `controller.py`. Reconnects on transient errors with `_WATCH_RECONNECT_DELAY = 2` s. Only yields on status change to suppress noisy MODIFIED events (e.g. label updates); a `DELETED` event on the controller Job ends the stream (terminal), letting callers fall through to their own fallback instead of hanging. Status is mapped via the shared `workflow_state._job_status_and_completion()` helper rather than a separate copy.

- **`src/seekr_chain/backends/k8s/workflow_state.py`** — `get_workflow_state()` was split into a thin fetch wrapper plus a pure `build_workflow_state(workflow_id, job, jobsets, pods)` tree-builder with no API calls. The individual fetch calls (`read_workflow_job`, `list_jobsets`, `list_pods`) were made public (dropped the leading underscore) so other callers can seed a cache with the same calls. `get_workflow_job_status()` now catches a 404 on the controller Job and returns `WorkflowStatus.UNKNOWN` instead of raising.

- **`src/seekr_chain/backends/k8s/watched_state.py`** (new) — `WatchedWorkflowState`: a watch-driven replacement for polling `get_workflow_state()`. Seeds a cache with one synchronous fetch of Job + JobSets + Pods (capturing each `resourceVersion`), then runs three daemon threads (one per resource kind) that watch from that point forward, applying ADDED/MODIFIED/DELETED events to the cache and rebuilding a `WorkflowState` via `build_workflow_state()` on every change. Each watch call passes `timeout_seconds=_WATCH_RECONNECT_DELAY` so a quiet stream still returns periodically and the thread notices `stop()` promptly instead of blocking indefinitely on `w.stream()`. Same `start`/`stop`/`latest`/`wait_for_first`/context-manager shape as `BackgroundStateFetcher`, plus `wait_for_update(timeout) -> bool` that blocks on a change event instead of sleeping.

- **`src/seekr_chain/backends/k8s/k8s_workflow.py`** (`follow()`) — rewritten to drive its live display from `WatchedWorkflowState` instead of `BackgroundStateFetcher`. The loop renders the latest snapshot, spawns log-follow threads for newly-running pods, then blocks on `watcher.wait_for_update(timeout=1.0)` — woken immediately by any Job/JobSet/Pod change, with a 1 s fallback in case an event was missed. This replaced the earlier design (see below) of running `watch_status()` in a `queue.Queue` daemon thread and polling `get_detailed_state()` every 2 s for pod discovery — now polling is eliminated entirely and *all* of Job/JobSet/Pod state is watch-driven, not just the top-line status.

- **`src/seekr_chain/wait.py`** — replaced the sequential polling loop with `ThreadPoolExecutor` + `as_completed`. Each job gets one thread running `_watch_to_completion()`. Multiple jobs are watched concurrently and `wait()` returns as soon as all finish. `_watch_to_completion` falls back to `job.get_status()` if the stream ends without a finished status (e.g. Job deleted mid-watch); with the `DELETED`-terminal fix above and the `get_workflow_job_status()` 404 handling, this fallback now actually runs and returns `WorkflowStatus.UNKNOWN` rather than hanging or raising. `poll_interval` parameter is kept for API compatibility.

### Key decisions

- **Generator over callback**: `watch_status()` is a generator that yields status changes. This is idiomatic Python, easy to test, and works naturally in both `follow()`'s watch loop and `wait()`'s `as_completed` pattern.
- **Non-abstract default**: The base class provides the polling fallback so `LocalWorkflow` and any future backends get `watch_status()` for free without implementing it.
- **Deduplication on yield**: Both the base-class fallback and the k8s override only yield when status actually changes (`status != last_status`), keeping the interface noise-free for callers.
- **`max(1, len(jobs))` guard**: Prevents `ThreadPoolExecutor(max_workers=0)` on an empty job list.
- **Pure builder, shared fetch helpers**: extracting `build_workflow_state()` and exposing `read_workflow_job`/`list_jobsets`/`list_pods` let `WatchedWorkflowState` reuse `get_workflow_state()`'s exact fetch/build logic for its one-time seed, avoiding a second implementation.
- **Single lock, single cache**: `WatchedWorkflowState` keeps `job`/`jobsets`/`pods` under one `threading.Lock` and rebuilds the whole `WorkflowState` from scratch on every event, rather than trying to patch the tree incrementally — simpler and cheap enough at this scale.

### Gotchas

- `follow()`'s watcher watches **Job + JobSets + Pods** (via `WatchedWorkflowState`) — the full per-step/per-role/per-pod tree is watch-driven, not just workflow-level status. `wait()`/`watch_status()` remains **Job-only** by design (it only needs the coarse terminal status, not per-step detail).
- `WatchedWorkflowState.stop()` still can't interrupt a watch thread instantly — `timeout_seconds` on each stream bounds the wait to roughly `_WATCH_RECONNECT_DELAY` seconds, but on a daemon thread this is a latency/tidiness concern only, not correctness (the process exiting kills the threads regardless).
- `kubernetes.watch.Watch` uses the standard k8s watch mechanism. The `resource_version=""` initial value means start from the current state; omitting it in subsequent reconnects (pass the last-seen rv) means resume without gaps. `410 Gone` resets to `""` and re-lists.
- A `DELETED` event on the controller Job is terminal for `watch_status()` — it ends the generator rather than being silently skipped, so `wait()`'s fallback-to-`get_status()` path is reachable.
