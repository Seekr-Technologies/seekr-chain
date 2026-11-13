# Task: fix-token-refresh

**Status**: in-progress
**Branch**: fix-token-refresh
**Created**: 2026-11-13 10:40

## Objective

## Problem

seekr-chain 0.35.1 caches kubeconfig loading and Kubernetes API clients in `seekr_chain/k8s_api.py`. This is reasonable for a long-running service, but Python Kubernetes 35.0.0 reads kubeconfig `tokenFile` credentials once without establishing an expiry. Its existing refresh hook therefore does not reread that file.

When Kubernetes rotates a projected service-account token, the cached client continues sending the old token. Once that token expires, later submissions from the same API worker can fail authentication despite a valid token on disk. This was reproduced offline: token A remained in the cached client's authorization header after the file changed to token B; a fresh client read B.

The new DEV/STAGE deployment configuration uses the standard pod token file. PROD uses external Secrets whose expiry/rotation policy is unknown; do not assume production immunity. Metaflow avoided cross-request caching by launching fresh submission subprocesses per request.

## Proposed fix

Keep client caching. Correct token-file authentication centrally in `seekr_chain/k8s_api.py`:

1. When the active kubeconfig authentication source is `tokenFile`, install a scoped `Configuration.refresh_api_key_hook` for the clients created by this provider.
2. On each authenticated request, reopen/read the token file and update that configuration's `api_key["authorization"]` with the current Bearer token. Reopen by path rather than retaining a file descriptor so projected-file replacement is observed.
3. Preserve relative-path resolution and existing authentication precedence. Leave inline-token, exec-plugin, certificate, and other authentication/refresh behavior unchanged.
4. Ensure all cached API client types receive the rotation-aware configuration. Do not reset process-global clients or reload global kubeconfig on every submission; callers may be concurrent or use other cluster configurations.
5. If the file is unreadable or empty, fail clearly without exposing credentials. Do not silently retain an unusable credential or log token values.

This avoids guessing JWT expiry, introducing long-lived credentials, or relying on worker restarts. If the implementation uses in-cluster authentication instead, it must explicitly select that mode and preserve existing remote-kubeconfig behavior—do not infer it merely because a service runs inside Kubernetes.

## Focused regression

Use the real cached provider/client with a temporary token-file kubeconfig; no live cluster is needed:

- Initialize once with token A; verify the outgoing auth header.
- Replace the token file with B; issue another authenticated operation through the **same client** and verify B is sent.
- Cover projected-style atomic/symlink replacement, empty/missing token files, and unchanged server/CA settings.
- Verify non-tokenFile authentication retains its existing behavior, including exec refresh where supported.

Use a small transport receiver to observe actual request headers rather than mocking the refresh hook itself. Publish the corrected chain release and update the consuming application dependency before enabling the new local-auth deployment wiring.

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
