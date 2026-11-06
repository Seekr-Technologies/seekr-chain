# Task: nix-closure-path

**Status**: complete
**Branch**: nix-closure-path
**Created**: 2026-11-06 17:08

## Objective

Avoid repeated submit-time Nix evaluation in ephemeral CI jobs by allowing users
to commit a hash-identified closure-path cache, shared across YAML configs and
Python-generated workflows.

## Summary

### Decision

Use an opt-in, additive JSON cache populated during ordinary submission, rather
than a separate lock-generation command or a strict fail-on-stale manifest.
Changed inputs add entries; old entries remain. No configuration means existing
behavior is unchanged.

- Configure flat `nix_closure_cache = "chain-nix-lock.json"` in
  `.seekrchain.toml`, consistent with existing project configuration.
- Environment/.env: `SEEKRCHAIN_NIX_CLOSURE_CACHE`.
- Explicit overrides: `chain submit --nix-closure-cache PATH`,
  `launch_workflow(..., nix_closure_cache=PATH)`, or
  `launch_k8s_workflow(..., nix_closure_cache=PATH)`.
- TOML paths resolve against the declaring file; env and explicit paths resolve
  against CWD. Configuration remains loaded once at import time.

### Implementation and rationale

- `src/seekr_chain/nix_closure_cache.py` owns version-1 persistence: entries
  keyed by the existing SHA-256 memo key (filtered source digest, normalized
  relative expression, attribute, target system), with readable input metadata
  and validated absolute Nix store paths.
- Persistent lookup precedes existing local/remote memos in
  `src/seekr_chain/nix_utils.py`. Memo hits backfill the file; successful
  evaluation records an entry before binary-store checks or cluster submission.
  A fresh-CI persistent hit requires no local Nix installation.
- File writes use a stable POSIX `.lock` sidecar, re-read/merge under the lock,
  and same-directory atomic replacement. Sorted JSON with no timestamps makes
  diffs deterministic. Hits do not write. Malformed/unsupported files fail
  explicitly rather than being overwritten.
- The selected cache, `.lock` sidecar, and `.tmp-*` files are excluded from
  dedicated Nix source hashing, evaluation, and build staging, preventing
  self-invalidation. General code uploads retain their existing semantics.
- Path selection is wired through `user_config.py`, `cli.py`, public launch
  APIs, the K8s launcher, and `nix_resolution.py`. Existing binary-store
  checks, build injection, and warm-node behavior are preserved.
- Usage and operational caveats are documented in
  `docs/guides/nix-mode.md` and `docs/reference/configuration.md`.

### Gotchas and boundaries

The cache records an input-to-output-path assertion, not binary availability.
Trust committed entries like build metadata. Locked flakes are recommended:
source hashing cannot detect impure external state or unlocked remote changes.
Curate Nix source filters to avoid invalidation from unrelated application edits.
Commit the JSON but gitignore writer artifacts. POSIX file locking targets the
existing Linux/macOS submit environments. No pruning, forced-refresh command, or
strict/read-only miss mode is introduced; removing persistent entries alone can
still reuse existing local/remote memos.

### Validation

- `uv run pytest tests/unit -q --tb=short --color=no`: **1,029 passed**.
- Ruff lint and formatting checks passed; `git diff --check` passed.
- New tests cover concurrent process writers, stable formatting, malformed
  files, changed inputs, attribute/system/expression variants, self-exclusion,
  missing binary-cache staging, memo backfill, shared Python configs, default
  and override selection, CLI forwarding, TOML path bases, and evaluation
  failures.
- Manual smoke test evaluated a real dependency-free Nix derivation, then
  resolved the same source through the persistent cache with evaluation
  disabled. No live-cluster submission was performed.
