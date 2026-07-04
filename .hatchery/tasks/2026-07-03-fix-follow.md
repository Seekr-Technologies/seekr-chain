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

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
