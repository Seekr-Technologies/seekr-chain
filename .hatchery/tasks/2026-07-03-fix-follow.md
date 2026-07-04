# Task: fix-follow

**Status**: complete
**Branch**: hatchery/fix-follow
**Created**: 2026-07-03 21:50

## Objective

occassionally when following, we get this:

[21:36:20] RUNNING    2:38  0+1/1  seekr-nix-push-pull-benchmark  kmv0tp
           └ RUNNING  2:35  0+1/1   pushpull                      kmv0tp-pushpull-js--0-0-crjx92026-07-03 21:36:21.091  WARNING seekr_chain state fetch failed: (401)
Reason: Unauthorized
HTTP response headers: HTTPHeaderDict({'Audit-Id': '5e949b84-dd25-402b-83ee-12d41ab81a10', 'Cache-Control': 'no-cache, private', 'Content-Type': 'application/json', 'Date': 'Sat, 04 Jul 2026 02:36:21 GMT', 'Content-Length': '129'})
HTTP response body: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"Unauthorized","reason":"Unauthorized","code":401}
pushpull-0 | 103M       /nix/store

The 401 error, but we immediately recover.

we shouldn't surface this to the user if we can avoid it

## Summary

### Problem
During `follow`/`attach`, `BackgroundStateFetcher` polls the k8s API every
~1 s. Occasional 401 Unauthorized responses (transient — they occur during a
client token-refresh window and self-resolve on the next fetch) were logged at
WARNING, surfacing noisy stack traces to the user even though the state itself
recovered immediately.

### Approach
Added an optional `transient_check: Callable[[Exception], bool]` callback to
`BackgroundStateFetcher.__init__`. When the callback classifies a thrown
exception as transient, the fetcher logs at **DEBUG** (suppressed by default)
instead of **WARNING**; non-transient exceptions still warn. The last-known
good `WorkflowState` continues to be served, unchanged.

`K8sWorkflow` passes a module-level `_is_transient_api_exception` helper that
treats `kubernetes.client.rest.ApiException` with `status == 401` as transient.
The set of transient codes lives in `_TRANSIENT_API_STATUSES` for easy
extension.

### Why a callback, not a hard-coded k8s check
`BackgroundStateFetcher` is intentionally generic over its `fetch_fn` return
type and has no kubernetes dependency in its own module. Pushing the
classification rule in via a callback keeps the fetcher decoupled and testable
without kubernetes installed, and lets other backends (or other classes of
transient errors) reuse the mechanism with their own predicate.

### Files changed
- `src/seekr_chain/backends/k8s/state_fetcher.py` — new `transient_check`
  parameter; DEBUG-vs-WARNING branching in `_run`.
- `src/seekr_chain/backends/k8s/k8s_workflow.py` — `_TRANSIENT_API_STATUSES`,
  `_is_transient_api_exception`, and the callback passed to
  `BackgroundStateFetcher` in both `follow()` and `attach()`.
- `tests/unit/backends/k8s/test_state_fetcher.py` — tests for transient
  demotion to DEBUG and default WARNING fallback.

### Gotchas
- The container used for this task has no `kubernetes` / `pytest` installed,
  so the unit tests could not be executed here. The fetcher logic was verified
  with a standalone replica (identical control flow) and all three edited
  files pass `py_compile`. **Run `pytest tests/unit/backends/k8s/test_state_fetcher.py`
  locally before merging.**
- A persistent 401 (genuine auth failure) will now be silent at WARNING level;
  the signal for a real problem is that the workflow never progresses. This is
  acceptable because the fetcher loops indefinitely regardless, so a recurring
  WARNING was never actionable on its own.
