# Task: cluster-secrets

**Status**: complete
**Branch**: hatchery/cluster-secrets
**Created**: 2026-04-21 14:00

## Objective

Update secrets so we can:

- Load secrets from the cluster
- Pick up secrets from the user's `.env` or environment variables

## Context

The `secrets` field on `WorkflowConfig` previously only supported inline key/value pairs
(`dict[str, str]`), requiring users to hardcode sensitive values in their YAML configs.
The docs had explicit "coming soon" placeholders for both env and cluster secret sources.

## Summary

### Final design: `dict[str, SecretValue]` with discriminated union

`secrets` is a dict where each key is the injected environment variable name (naturally
preventing duplicates) and each value is one of three types:

```yaml
secrets:
  MY_KEY: "literal"                       # inline — plain string
  WANDB_KEY: {env: true}                  # from local env; var name == key
  HF_TOKEN:  {env: HF_TOKEN}             # from local env; explicit var name
  API_TOKEN:
    secretRef:
      name: my-k8s-secret                 # backend secret store reference
      key: token                          # optional; defaults to dict key
```

`EnvSource` and `SecretRefSource` are proper discriminated union members — each has
exactly one required field (`env` / `secretRef`) that identifies the type. Pydantic's
smart union tries each left-to-right; no explicit `type` discriminator needed.

The `secretRef` vocabulary is intentionally backend-agnostic (not `secretKeyRef`,
which is k8s-specific), so it can map naturally to SLURM, local, or other backends
in the future.

### Design decisions made during the task

1. **List → dict**: An early iteration used `list[SecretEntry]` (each entry had a `key`
   field). Reverted to dict because dicts naturally enforce key uniqueness and the dict
   key *is* the env var name — the list structure was redundant and allowed duplicate
   env var names.

2. **`from_env`/`from_cluster` → `env`/`secretRef`**: Adopted the k8s `name`/`valueFrom`
   convention for familiarity, but used `secretRef` (not `secretKeyRef`) to stay
   backend-agnostic for future SLURM/local backends.

3. **Inline string remains `str`**: Plain string values remain valid and backward-compat.
   The existing `dict[str, str]` format still works as-is — no validator needed.

### Key implementation details

- **Env secrets** (`EnvSource`): resolved in `_resolve_env_secrets()` in
  `launch_argo_workflow.py`. Uses `dotenv.find_dotenv(usecwd=True)` + `dotenv_values()`
  for `.env` loading; actual env vars take priority. Collects all missing vars and raises
  one `RuntimeError` before any K8s calls.
- **Secret store refs** (`SecretRefSource`): values are never read or copied.
  `_create_workflow_secrets()` emits a `secretKeyRef` pointing at the original secret
  name (`secretRef.name`). The secret must exist in the same namespace when the job runs.
- **Inline strings** (`str`): values go into the per-workflow K8s Secret alongside S3
  credentials, then injected via `secretKeyRef` pointing at the workflow-scoped secret.

### Files changed

| File | Change |
|------|--------|
| `src/seekr_chain/config.py` | Added `SecretRef`, `EnvSource`, `SecretRefSource`, `SecretValue`; updated `WorkflowConfig.secrets` type |
| `src/seekr_chain/backends/argo/launch_argo_workflow.py` | Added `_resolve_env_secrets()`; updated `_create_secrets()` and `_create_workflow_secrets()` |
| `tests/unit/test_config.py` | `TestSecretConfig` covering all three types |
| `tests/unit/test_manifest_rendering.py` | `SecretRefSource` rendering test |
| `tests/unit/test_secrets_resolution.py` | New file: 10 tests for `_resolve_env_secrets()` |
| `docs/guides/secrets.md` | Full rewrite with all three source types |

### Gotchas for future agents

- `SecretRef.key` defaults to the dict key at resolution time (implemented in
  `_create_workflow_secrets`, not in the model itself — the model stores `None`).
- `EnvSource.env: bool` means "use the dict key as the var name". `bool` comes before
  `str` in Python's MRO, so `True`/`False` won't accidentally be coerced to strings.
- When adding a new backend, `SecretRefSource` needs a backend-specific resolution
  strategy; `EnvSource` and inline strings should work unchanged.
