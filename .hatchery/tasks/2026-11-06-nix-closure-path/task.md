# Task: nix-closure-path

**Status**: in-progress
**Branch**: nix-closure-path
**Created**: 2026-11-06 17:08

## Objective

Every time I submit a chain job with a nix closure, it has to re-eval the closuure to compute the path.

on my local machine, it can use a local cache, so it's fast on subsequent steps.

however, in CI, it is not fast. Each CI job has to re-compute it.

Can we come up wiht a way that a user can check in a hash-identified closure path manifest of some sort, that chain can use to lookup the closure path?

What would such a solution look like?

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
