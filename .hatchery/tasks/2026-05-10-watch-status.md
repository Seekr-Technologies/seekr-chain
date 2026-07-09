# Task: watch-status

**Status**: complete
**Branch**: hatchery/watch-status
**Created**: 2026-05-10 09:16

## Objective

Replace fixed-interval polling in `wait()` and `follow()`/`attach()` with
event-driven status watching via the Kubernetes Watch API, eliminating
polling latency and the risk of an unbounded hang on a dropped connection.

## Context

`wait()` polled `get_status()` every 10 s; `follow()`/`attach()` polled
`get_detailed_state()` every 1-2 s. The in-cluster `controller.py` already
used `kubernetes.watch.Watch` for zero-latency event detection — this
extends the same approach to the client side.

## Current design

- **`Workflow.watch_controller_status()`** (`workflow.py`) — abstract-ish
  base method: a generator yielding `WorkflowStatus` on change, returning
  when finished. Default implementation polls `get_status()` every 2 s so
  backends without a watch API (e.g. `LocalWorkflow`) still work. `K8sWorkflow`
  overrides it with a real watch.
- **`watched_state.py`** — `ReconnectingWatcher` is the single generic watcher
  class. It's driven by one or more `WatchSpec`s (one per K8s resource kind:
  seed function, watch/list call, key/resourceVersion extractors) and a
  `project` callback that turns the per-kind caches into whatever snapshot
  the caller wants. Two factories configure it:
  - `workflow_state_watcher()` — Job + JobSets + Pods → `WorkflowState`, used
    by `follow()`/`attach()` for the full per-step/pod live display.
  - `controller_status_watcher()` — Job only → `WorkflowStatus`, used by
    `watch_controller_status()` (and hence `wait()`), which only needs the
    coarse terminal status and may be watching many jobs concurrently — one
    lightweight Job-only watch per job keeps that cheap.
  Uses the standard "list-then-watch" pattern: one synchronous seed fetch
  captures each kind's resourceVersion, then one daemon thread per kind
  watches forward from there, applying ADDED/MODIFIED/DELETED events into a
  cache dict. `latest()` lazily rebuilds the projected snapshot behind a
  dirty flag, coalescing bursts of events into one rebuild.
- **Resilience**: each watch reconnects with exponential backoff
  (`_WATCH_BACKOFF_BASE_SECONDS`/`_WATCH_BACKOFF_MAX_SECONDS`,
  `_WATCH_MAX_ATTEMPTS`), treats HTTP 410 (Gone) as a routine re-list rather
  than a failure, and escalates to `WatchStalledError` after sustained
  failure across all watched kinds. Callers catch that and print a short
  reconnect hint (`print_reconnect_hint`) instead of a raw traceback.
- **`wait.py`** — watches multiple jobs concurrently via
  `ThreadPoolExecutor`/`as_completed`, one thread per job running
  `_watch_to_completion()`. Falls back to `job.get_status()` if a watch
  stream ends without a finished status (e.g. controller Job deleted
  mid-wait).
- **`workflow_state.py`** — `build_workflow_state(workflow_id, job, jobsets,
  pods)` is a pure tree-builder (no API calls), shared by the one-shot
  `get_workflow_state()` and by `workflow_state_watcher()`'s projection.
  `read_workflow_job()`/`list_jobsets()`/`list_pods()` are the individual
  fetch calls, public so a watcher can reuse them to seed its cache.

## Gotchas / learnings for future agents

- **A dead socket doesn't raise on its own.** The K8s API server's
  `timeout_seconds` only asks *it* to close the stream after a quiet period —
  it does nothing if the connection dies at the network layer (VPN drop,
  laptop sleep). Without a client-side `(connect, read)` socket timeout
  (`_WATCH_REQUEST_TIMEOUT`), a watch thread blocks forever with no exception,
  so the failure is never recorded or escalated. If you touch the watch
  loop, keep this timeout — it's the only thing that turns a silent hang
  into a retryable failure.
- **410 Gone is not a failure — it's a routine re-list signal.** A
  resourceVersion going stale is expected (etcd compaction) and should reset
  to seeding from `""` rather than counting toward the backoff/attempt limit.
  Conflating the two makes `wait()`/`follow()` escalate to `WatchStalledError`
  on a totally healthy cluster.
- **Set `_first_ready` only *after* starting the watch threads**, not before
  and not synchronously inside `start()` before the seed completes. Setting
  it too early lets `wait_for_first()` return a stale/empty snapshot to a
  caller racing the watcher's own setup.
- **`connection_status()` must report the longest-running failure streak**
  across all watched kinds, not just any one kind's current state — with
  multiple `WatchSpec`s (job/jobsets/pods), one kind can be reconnecting
  cleanly while another has been failing for the full backoff window; the
  banner shown to the user should reflect the worse one.
- **Job-existence must be derived from `status is None`, not a separate
  "does the job exist" flag.** `controller_status_watcher()`'s projection
  returns `None` when the Job cache is empty (deleted or never existed) —
  callers (`watch_controller_status()`, `follow()`) must treat `None`/
  `WorkflowStatus.UNKNOWN` as terminal-but-not-finished and return/break
  rather than looping on `wait_for_update()` forever, since no further
  events will ever arrive for a resource that's gone.
- **Don't let `ThreadPoolExecutor`'s context-manager `__exit__` block
  `wait()`'s return.** A plain `with ThreadPoolExecutor() as executor:` calls
  `shutdown(wait=True)` on exit, which blocks until *every* submitted future
  finishes — including ones still watching an unrelated healthy job for
  hours. `wait()` needs to fail fast when one job's watch hits
  `WatchStalledError`, so it manages the executor manually and calls
  `shutdown(wait=False, cancel_futures=True)`.
- **`list_jobsets()`/`read_workflow_job()` must propagate errors, not
  swallow 404 into an empty result.** An empty JobSet list is a valid state
  (no steps yet); a 404 means the caller can't reach the API and needs to
  know that, not silently proceed as if nothing exists. This distinction
  matters most on the watch seed path, where a swallowed error would seed
  the cache empty and never surface the underlying connectivity problem.
- **When merging in unrelated `main` changes that touch the same functions**
  (e.g. a later hotfix to the old polling code), check whether the fix is
  still meaningful under the new design before blindly taking "our side" —
  e.g. main's `follow()` fix to break the display loop on
  `WorkflowStatus.UNKNOWN` was still correct and worth porting forward, but
  main's private `_job_status_and_completion`/404-swallowing
  `_list_jobsets_by_step` were superseded by this branch's deliberate
  public-rename and error-propagation fixes and should not be reintroduced.
