# Task: fix-follow

**Status**: in-progress
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
recovered immediately. A reviewer also flagged the risk of swallowing a *persistent*
401 (e.g. from a VPN disconnect) silently forever.

### Approach
Two-part policy in `BackgroundStateFetcher`:

1. **Swallow list via `transient_check`** — an optional
   `Callable[[Exception], bool]`. When it returns truthy for a thrown exception,
   the fetcher logs at **DEBUG** (silent) instead of **WARNING**. Use this for
   self-resolving blips (401 during a token-refresh window).

2. **Continuous-error escalation via `error_tolerance`** (default 30 s) — the
   fetcher tracks a streak of continuous fetch failures (any kind). While within
   the tolerance window, transient errors stay at DEBUG and non-transient errors
   warn each cycle. If failures persist continuously past the tolerance (no
   successful fetch in between), the fetcher records a `ContinuousFetchError`
   carrying the unique exceptions seen during the streak, stops its thread, and
   re-raises from `latest()` / `wait_for_first()` so the follow session exits
   rather than running forever against stale state. A single successful fetch
   resets the streak.

`K8sWorkflow` passes `_is_transient_api_exception` (401 `ApiException` → True)
to both `follow()` and `attach()`, with the default 30 s tolerance.

### Why raise from `latest()`, not on the daemon thread
The fetcher runs `fetch_fn` on a daemon thread; raising there would kill the
thread silently with no propagation to the follow loop. Instead, a fatal streak
sets `_fatal_error` and stops the thread; the next `latest()` call in the follow
loop re-raises it on the caller's thread so the session exits cleanly.

### Behavior summary
- **Single 401 blip (recovers next cycle):** silent (DEBUG). Streak resets on success.
- **VPN disconnect (401s continuous > 30 s):** escalates — `ContinuousFetchError`
  raised from `latest()`, follow session exits with a message listing the unique errors.
- **Non-swallow-list error (e.g. 500):** WARNING each cycle; after 30 s continuous,
  escalates to `ContinuousFetchError`.
- **Recovery mid-streak:** success resets the window; a later blip starts fresh.

### Files changed
- `src/seekr_chain/backends/k8s/state_fetcher.py` — new `ContinuousFetchError`,
  `transient_check` + `error_tolerance` params, streak tracking, fatal-error
  propagation via `latest()`/`wait_for_first()`.
- `src/seekr_chain/backends/k8s/k8s_workflow.py` — `_TRANSIENT_API_STATUSES`,
  `_is_transient_api_exception`, callback passed to `BackgroundStateFetcher` in
  `follow()` and `attach()`.
- `tests/unit/backends/k8s/test_state_fetcher.py` — tests for swallow-list
  demotion, single-blip silence, continuous-transient escalation,
  continuous-non-transient escalation, and streak reset on success.
