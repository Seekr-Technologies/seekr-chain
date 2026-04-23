# Secrets Management

Secrets allow you to securely pass sensitive information — API keys, credentials, tokens — into your jobs as environment variables.

## Secret sources

`secrets` is a dict where each key is the environment variable name injected into the container, and each value specifies where the secret comes from:

| Value type | Format | Where the value comes from |
|------------|--------|---------------------------|
| Inline | plain string | Written directly in the config |
| Environment | `env: ...` | Read from your local shell or `.env` file at submit time |
| Secret store | `secretRef: ...` | Referenced from the backend's secret store (e.g. a Kubernetes Secret) — never copied |

### Inline secrets

The simplest form — the value is a plain string in your config:

```yaml
secrets:
  MY_API_KEY: "abc123"
```

!!! warning
    Avoid committing inline secrets to source control. Prefer the environment or secret store sources below for anything sensitive.

### Environment secrets

The value is read from your local environment (or a `.env` file) when you run `chain submit`. The resolved value is then stored transiently in the per-job secret store entry for the duration of the job.

```yaml
secrets:
  # Shorthand: env var name matches the secret key
  WANDB_KEY:
    env: true

  # Explicit: read a different env var name
  MY_KEY:
    env: SOURCE_VAR
```

If the variable is not set in your environment or `.env` file, `chain submit` will exit with a clear error before submitting the job.

**`.env` file support**: seekr-chain walks up from your current working directory looking for a `.env` file. Values defined there are picked up automatically. Environment variables take priority over `.env` values.

### Secret store secrets

Reference a secret that already exists in the backend's secret store (e.g. a Kubernetes Secret). The container receives a direct reference to the named secret — the value is never read, copied, or logged by seekr-chain.

```yaml
secrets:
  # Key name in the secret store matches the env var name
  API_TOKEN:
    secretRef:
      name: my-credentials

  # Key name in the secret store differs from the env var name
  API_TOKEN:
    secretRef:
      name: my-credentials
      key: token
```

The secret must exist in the same namespace as the workflow when the job runs.

## Mixing sources

All three sources can be combined freely:

```yaml
secrets:
  INLINE_KEY: "some-literal-value"

  WANDB_KEY:
    env: true

  HF_TOKEN:
    env: HF_TOKEN

  DB_PASSWORD:
    secretRef:
      name: my-db-credentials
      key: password
```

## Secret lifecycle

- On job launch, seekr-chain creates a backend secret entry scoped to the workflow ID containing all inline and environment-sourced secrets (plus S3 credentials).
- Secret store references are never copied into the workflow-scoped entry.
- Secrets are injected as environment variables in every step container.
- On launch, seekr-chain also deletes any workflow-scoped secrets it created more than **7 days** ago.
