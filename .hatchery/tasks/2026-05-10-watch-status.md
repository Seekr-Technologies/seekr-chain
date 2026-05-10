# Task: watch-status

**Status**: in-progress
**Branch**: hatchery/watch-status
**Created**: 2026-05-10 09:16

## Objective

When we watch a job status (either in wait or when following logs), we currently have to `poll` the `get_status` method on the job.

Some backends (e.g. k8ss) have better ways to do this. That is, we can set up a k8s watcher for status changes for pods matching a given label. This is similar to how the `controller.py` is handling watching for pod status changes.

This would completely eliminate our polling latency when `wait` and `follow` on a job.

Please plan out the implementation of:

- Adding a `watch_status` or similar method on our backend base class
- Filling this out in k8s with an optimized version
- Ensureing we use this when following logs too

We also need to make sure this can function in a 'non blocking' way. For example, if we are waiting on multiple jobs, etc.

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
