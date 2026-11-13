# Task: fix-token-refresh

**Status**: complete (implementation; release rollout pending)
**Branch**: fix-token-refresh
**Created**: 2026-11-13 10:40

## Objective

Keep cached Kubernetes clients usable after kubeconfig token-file rotation in a long-lived worker, without changing other authentication or process-global configuration.

## Summary

### Evidence and decision

Reproduced offline on Python 3.12 / Kubernetes 35.0.0 before implementation: a local HTTP receiver accepted synthetic token A, then rejected all three cached client types after atomic replacement with B because they still sent A (401). A fresh client read B (200). A prototype scoped reread hook made those same cached clients send B successfully, including projected-style `..data` symlink replacement. This models expiry by rejecting the old credential; no live-cluster expiration test was performed.

Keep client/configuration caching. Reopen the active token file by path on each authenticated request rather than guessing JWT expiry, retaining descriptors, restarting workers, or switching implicitly to in-cluster authentication.

### Implementation

- `src/seekr_chain/k8s_api.py`: load a provider-owned `Configuration` using upstream `KubeConfigMerger` and a narrow `KubeConfigLoader` subclass. Preserve merged-file path provenance and credential persistence. All three cached API types explicitly receive this configuration; global defaults and unrelated clients are untouched.
- Override only the token-file authentication branch. Upstream still selects auth-provider before token, token before exec, and inline token before tokenFile. Non-file auth retains upstream hooks.
- Reread hooks observe overwrite, atomic rename, and projected-volume symlink replacement. Empty/whitespace, missing, unreadable, or undecodable files raise credential-free errors before HTTP transport; successful later reads recover without reconstructing the client. Initial loading also fails safely.
- `tests/unit/backends/k8s/test_token_refresh.py`: real generated API operations against a local HTTP receiver cover all three cached types, rotation/replacements, failure/recovery, auth precedence, exec expiry refresh through a real subprocess, concurrent requests, global isolation, merged relative paths, and server/CA/TLS/certificate settings.
- Existing provider caching/error tests in `test_k8s_api.py` and `test_error_messages.py` now patch the provider configuration-loading seam.
- The exploratory script was superseded by permanent regression tests.
- At user request, the refresh callback uses `functools.partial` on module-level `_refresh_token_file`, binding path/key without a closure or dedicated callable class. Request-time rereading and caching behavior are unchanged.

### Compatibility / gotchas

The extension uses protected upstream loader methods to avoid duplicating auth precedence. Recheck the regression suite when upgrading Kubernetes. Kubernetes 32/35 populate `api_key["authorization"]`; 36 populates `api_key["BearerToken"]`. The hook updates whichever key upstream populated.

HTTP receiver tests verify actual headers without mocking hooks. Separate HTTPS configuration comparisons verify server, CA, verification, TLS name, and certificate/key settings against upstream; they do not perform a TLS handshake.

For the minimum-version test only, cryptography 50.0.2's native module crashed on import with an illegal instruction in this container; using cryptography 44.0.3 allowed the check. No dependency or lockfile changes were made, and the environment was restored with `uv sync`.

### Validation

- Full unit suite: **1050 passed**, separately on Kubernetes **35.0.0** and locked **36.0.2**.
- Focused provider/auth/error suite: **78 passed** on **32.0.1**, **35.0.0**, and **36.0.2**.
- Negative control: substituting upstream's unmodified token loading/config hook made all **9 rotation regressions fail** with the stale header.
- Ruff lint/format and `git diff --check` passed.
- Partial-callback follow-up: focused suite **78 passed** on locked **36.0.2**; Ruff lint/format and diff checks passed.
- No live cluster required or used.

### Rollout

Publish the corrected chain release and update the consuming application's dependency **before enabling local-auth deployment wiring**. Publication and the consuming-application change were not performed here. DEV/STAGE use projected pod tokens; PROD uses external Secrets with unknown expiry/rotation policy and must not be assumed immune.
