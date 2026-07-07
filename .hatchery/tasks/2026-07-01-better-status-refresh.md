# Task: better-status-refresh

**Status**: complete
**Branch**: hatchery/better-status-refresh
**Created**: 2026-07-01 08:32

## Objective

Our status lines when following a job refresh slowly (once every few seconds).

This is due to syncronyous calls to get workflow status.

Can we:

- run status/log retrieveal in the background
- continue updating the display every second so we see timers/clocks tick appropriately?

## Context

`K8sWorkflow.follow()` and `.attach()` both drove a display loop that
called `self.get_detailed_state()` inline. That call issues three
sequential Kubernetes API requests (`_read_workflow_metadata`,
`_list_jobsets_by_step`, `_group_pods_by_step_and_role`) inside
`get_workflow_state()` and takes 1–3 s under load, so the display only
advanced every 2–4 s and every on-screen timer stuttered.

## Summary

**Approach.** Decouple *fetching* from *rendering*. A new
`BackgroundStateFetcher` runs `get_detailed_state()` on a daemon thread
and publishes the latest `WorkflowState` under a lock; the display loop
in `follow()` / `attach()` re-renders whatever is currently latest every
second, so timers tick smoothly even while a fresh fetch is in flight.

**Key insight.** `format_duration(start, None)` in `src/seekr_chain/utils.py`
already computes end times against `datetime.now(UTC)`. That means
re-rendering the *same* `WorkflowState` object still advances every
duration on screen — no state mutation, no renderer changes needed.

**Header timestamp.** The bracketed `[HH:MM:SS]` in the header is the
current *local* wall-clock time at render (via
`datetime.now().astimezone()` in `_header_row`), not the state's
`captured_at`. Because the display loop re-renders once a second, the
clock ticks smoothly and matches the user's timezone. `captured_at`
remains on `WorkflowState` as snapshot metadata but is no longer
consumed by the renderer — nothing else uses it either.

**Files changed:**

- `src/seekr_chain/backends/k8s/state_fetcher.py` (new) —
  `BackgroundStateFetcher` context manager. Daemon thread waits on a
  `threading.Event` for its interval so `stop()` is prompt regardless
  of the interval length. Transient exceptions in `fetch_fn` are logged
  and swallowed; the last good snapshot keeps being served.
- `src/seekr_chain/backends/k8s/k8s_workflow.py` — `follow()` and
  `attach()` wrap the fetcher in a `with`, block once on
  `wait_for_first()`, then re-render `fetcher.latest()` every second.
  Existing log-follow-thread logic and stop conditions unchanged.
- `src/seekr_chain/backends/k8s/render_status.py` — `_header_row()`
  now uses `datetime.now().astimezone()` for the bracketed timestamp
  instead of `workflow_state.captured_at`, so the clock ticks every
  second in the user's local timezone.
- `tests/unit/backends/k8s/test_state_fetcher.py` (new) — 7 tests
  covering latest/first/error-resilience/idempotent stop/context-manager
  cleanup/prompt-shutdown-under-long-interval.
- `tests/unit/backends/k8s/test_status_rendering.py` —
  `test_header_includes_timestamp` now checks the timestamp *shape*
  since exact wall-clock isn't deterministic.

**Gotchas for future agents:**

- The renderer relies on `datetime.now()` in `format_duration` and
  `_step_time` (`render_status.py:117`). If a future change moves to
  pre-baked end times, the "re-render advances timers" property breaks
  and the fetcher alone won't smooth the display.
- `PlainLive.update()` is a no-op, so `--plain` mode is unaffected by
  this change — the fetcher runs but nothing is drawn.
- `_first_ready` is only set after a successful publish, so
  `wait_for_first()` truly guarantees `latest()` is non-None on all
  subsequent calls from that point.
- If the very first fetch hangs indefinitely, `wait_for_first()` will
  block forever (matches prior behavior; a caller that wants a bounded
  startup can pass `timeout=`).

**Verification.**

- `uv run pytest tests/unit/` — 454 passed.
- Manual smoke against a live cluster not performed in this worktree
  (no cluster access from the sandbox). Suggested for the merger:
  `chain submit --follow …` and confirm the header/step timers advance
  every second while `kubectl get pods` is under load.
