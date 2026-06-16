# Task: safe-secret-cleanup

**Status**: complete
**Branch**: hatchery/safe-secret-cleanup
**Created**: 2026-06-16 16:33

## Objective

chain automatically cleans up stale secrets on upload. However, some users or CI roles do not have list/delete permissions for secrets.

Please update the secret cleanup to try/except for the `list` and issue a warning if we can't clean up:

HTTP response headers: HTTPHeaderDict({'Audit-Id': 'eb746e72-1a18-408e-927e-154de78a14f3', 'Cache-Control': 'no-cache, private', 'Content-Type': 'application/json', 'X-Content-Type-Options': 'nosniff', 'X-Kubernetes-Pf-Flowschema-Uid': 'd1a26c27-d87e-4cf0-9143-bece1b6023ad', 'X-Kubernetes-Pf-Prioritylevel-Uid': 'f6cdd6b1-5b7b-405e-9326-56cf14277580', 'Date': 'Tue, 16 Jun 2026 21:28:04 GMT', 'Content-Length': '372'})
HTTP response body: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure","message":"secrets is forbidden: User \"ocid1.user.oc1..aaaaaaaao4ujfuoxhxsck7wp4u75knhc2o36sdxxhrr6mi7xdpxyjqmp3v6a\" cannot list resource \"secrets\" in API group \"\" in the namespace \"argo-workflows\": . Opc-Request-Id: <nil>","reason":"Forbidden","details":{"kind":"secrets"},"code":403}

## Summary

### What changed

`_create_secrets()` in `src/seekr_chain/backends/argo/launch_argo_workflow.py` now treats stale-secret cleanup as best-effort. The `v1.list_namespaced_secret(...)` call is wrapped in `try / except kubernetes.client.exceptions.ApiException`; on failure (e.g. RBAC 403 Forbidden) the function logs a `warning` with the status/reason and returns without deleting anything. The per-workflow secret upload (`create_namespaced_secret`) runs *before* this block and is unaffected.

### Key decisions

- **Wrap only the `list` call, not the whole cleanup block.** The existing inner `try/except` around `delete_namespaced_secret` (debug-level logging per failed delete) handles partial-permission cases where list works but a delete returns 403. Leaving it intact means one bad secret can't abort the loop while we still surface the bigger "I can't see anything" failure at warning level.
- **`logger.warning(...)` over `warnings.warn(...)`.** Matches the existing pattern at `launch_argo_workflow.py:195` (the s3-creds override warning in `_create_workflow_secrets`). The module already has `logger = logging.getLogger(__name__)`.
- **Use the fully-qualified `kubernetes.client.exceptions.ApiException`.** Same import path as the pre-existing inner `except` — no new imports needed.
- **`return` after the warning rather than guarding the delete loop with `if resp is not None`.** Cleanup is the last thing `_create_secrets` does, so the early return is the simplest control flow.

### Files changed

- `src/seekr_chain/backends/argo/launch_argo_workflow.py` — wrap list call.
- `tests/unit/test_secrets_resolution.py` — new `TestCreateSecretsCleanup` class with two tests covering the 403 path and the happy delete path. Mocks `k8s_utils.get_core_v1_api` and `logger` at the module scope.

### Gotchas / notes for future agents

- The cleanup block only targets secrets labeled `app=seekr-chain,managed-by=seekr-chain,type=workflow-secret` older than 7 days — it will not touch user-managed secrets even if RBAC were broader. Don't loosen the label selector.
- The unit tests construct a `WorkflowConfig` via `_config_with_secrets(None)` (no secrets), which means the `secrets` dict inside `_create_secrets` is empty and `create_namespaced_secret` is not called. If you ever need to test the upload + cleanup together, you'll have to mock `create_namespaced_secret` too and provide an s3_creds dict or an inline secret.
- The existing integration test `tests/integration/core/test_basic_job.py::TestBasic.test_secrets` runs against a cluster where the test account has full permissions, so it does *not* exercise the 403 path. The new unit tests are the only coverage for this branch.
