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

## Agreed Plan

1. **Add a `transient_check` callback to `BackgroundStateFetcher`** — an optional
   `Callable[[Exception], bool]`. When provided and it returns `True` for a given
   exception, the fetcher logs at **DEBUG** (suppressed) instead of **WARNING**.
   Default is `None` (all exceptions logged at WARNING — current behavior). This
   keeps the fetcher generic; it doesn't need to know about kubernetes.

2. **Pass a transient check from `K8sWorkflow`** — when constructing
   `BackgroundStateFetcher` in `follow()` and `attach()`, pass a callback that
   returns `True` for `kubernetes.client.exceptions.ApiException` with
   `status == 401` (transient auth/token-refresh blips). These are self-resolving
   via the next fetch cycle.

3. **Add unit tests** — verify that when `transient_check` classifies an exception
   as transient, it's logged at DEBUG (not WARNING), while non-transient exceptions
   are still logged at WARNING. Verify the last-good state survives a transient error.


## Progress Log

- [x] Add `transient_check` callback to `BackgroundStateFetcher` — demotes known-transient exceptions to DEBUG
- [x] Pass `_is_transient_api_exception` from `K8sWorkflow.follow()` and `attach()` (401 classified transient)
- [x] Add unit tests for transient suppression + default-warning fallback
- [x] Verify logic standalone (no kubernetes in container); files compile clean

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
