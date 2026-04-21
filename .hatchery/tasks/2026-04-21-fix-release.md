# Task: fix-release

**Status**: complete
**Branch**: hatchery/fix-release
**Created**: 2026-04-21 08:28

## Objective

The `feat: pod affinities` PR was supposed to create a release, but it did not.

## Context

Commit `b4996b6 feat: pod affinities (#16)` merged to main and should have triggered a v0.12.0 minor release (from v0.11.0), but the `compute-version` job printed:

```
Latest tag: v0.11.0
PR number: 
No PR found for this push, skipping release
```

## Summary

**Root cause:** The GitHub API endpoint `commits/:sha/pulls` has a brief propagation delay after a squash merge — the commit-to-PR association is not always immediately indexed when the push event fires. The pipeline received an empty array `[]`, which rendered as an empty string (not "null"), and bailed out.

**Fix:** `.github/workflows/release.yml` — replaced the single PR lookup with two layers of resilience:

1. **Retry loop** (3 attempts, 10s apart) — handles the common case of a short API propagation delay
2. **Commit-message fallback** — if all retries fail, parse the PR number from the squash-merge commit subject's `(#N)` suffix; this is always present for GitHub squash merges

The "skip if no PR found" logic is preserved for genuine direct pushes to main.

**Key detail:** When jq evaluates `.[0].number` on an empty array, `gh api --jq` produces empty output (not the string `"null"`). The existing null check caught it, but the fix needed to handle both cases in the retry/fallback code.

**File changed:** `.github/workflows/release.yml` (lines 44-72 in the new file, the `Compute version` step's PR-lookup block)

**No other files changed.** `release-preview.yml` is unaffected — it triggers on `pull_request_target` and gets the PR number directly from the event context.
