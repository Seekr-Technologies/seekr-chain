# Configuration

seekr-chain resolves configuration from multiple sources in priority order. Each setting uses the first non-null value found:

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (highest) | Environment variable | Shell env, CI/CD secrets, `export` |
| 2 | `.env` file | Walk up from CWD — local overrides, **should be gitignored** |
| 3 | `.seekrchain.toml` | Walk up from CWD — **committed team default** |
| 4 (lowest) | `~/.seekrchain.toml` | Personal global default |

## `.seekrchain.toml`

The recommended way to share a configuration default with your team is to commit a `.seekrchain.toml` file in your project root:

```toml
# .seekrchain.toml
datastore_root = "s3://my-bucket/seekr-chain/"
```

seekr-chain searches for this file by walking up from the current working directory, so it works from any subdirectory of your project.

## `~/.seekrchain.toml`

For personal defaults that apply across all projects (e.g. on your dev machine), add a `.seekrchain.toml` to your home directory:

```toml
# ~/.seekrchain.toml
datastore_root = "s3://my-personal-bucket/seekr-chain/"
```

## `.env` file

`.env` files are supported for compatibility with tools like `direnv` and Docker Compose. Use the `SEEKRCHAIN_DATASTORE_ROOT` key:

```bash
# .env  (add to .gitignore — this file is for local overrides only)
SEEKRCHAIN_DATASTORE_ROOT=s3://my-bucket/seekr-chain/
```

> **Note:** `.env` files are commonly used for secrets and are expected to be gitignored. For a committed team default, use `.seekrchain.toml` instead.

## Settings reference

| Setting | Env var | TOML key | Description |
|---------|---------|----------|-------------|
| Datastore root | `SEEKRCHAIN_DATASTORE_ROOT` | `datastore_root` | S3 path where job data is stored, e.g. `s3://my-bucket/seekr-chain/` |
| Controller ServiceAccount | `SEEKRCHAIN_SERVICE_ACCOUNT` | `service_account` | ServiceAccount for the controller pod; defaults to automatic detection. |
| Step ServiceAccount | `SEEKRCHAIN_STEP_SERVICE_ACCOUNT` | `step_service_account` | Optional ServiceAccount for step pods; when unset, Kubernetes uses the namespace default. |
| Datastore Kubernetes secret | `SEEKRCHAIN_DATASTORE_KUBERNETES_SECRET` | `datastore_kubernetes_secret` | Name of a pre-existing Kubernetes Secret holding S3 credentials; when set, step pods reference it instead of uploading your local AWS credentials. |
| Datastore endpoint URL | `SEEKRCHAIN_DATASTORE_ENDPOINT_URL` | `datastore_endpoint_url` | Custom S3 endpoint URL for the datastore, e.g. an OCI Object Storage endpoint `https://<ns>.compat.objectstorage.<region>.oraclecloud.com`; configures the local client and is injected into pods, overriding `S3_ENDPOINT_URL` from `datastore_kubernetes_secret` when set. |
| Datastore region | `SEEKRCHAIN_DATASTORE_REGION` | `datastore_region` | S3 region for the datastore, often required for SigV4 signing even against S3-compatible endpoints; configures the local client and is injected into pods, overriding `AWS_REGION` from `datastore_kubernetes_secret` when set. |
