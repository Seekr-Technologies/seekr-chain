# Task: consolidate-init-steps

**Status**: complete
**Branch**: hatchery/consolidate-init-steps
**Created**: 2026-04-29 09:13

## Objective

Combine 3 sequential init containers (download, unpack tar, inject shell) into a single init container to improve pod startup efficiency, backed by a custom Docker image with all required tools.

## Context

Every seekr-chain job pod previously ran 3 init containers before the main container:

1. **`download-assets`** (`amazon/aws-cli:2.25.11`): Download assets tarball from S3, upload pod metadata
2. **`unpack-assets`** (`alpine:3.22.0`): Unpack tarball, delete it, symlink role-specific assets
3. **`inject-shell`** (`busybox:1.37-uclibc`): Copy busybox to `/seekr-chain/bin/` so the main container (which may have no shell) gets sh/tee/touch/sleep/head/awk/cat

This caused 3 sequential image pulls on every pod start. Merging them into one init container reduces startup latency.

## Summary

### Approach

Built a custom `seekr-chain-init` image based on `alpine:3.22.0`. Alpine natively includes `busybox` (at `/bin/busybox`) and `tar`/`gzip`; only the AWS CLI v2 static binary needed to be added. The init script now runs as a single `&&`-chained command under `set -e`.

### Files Changed

| File | Change |
|---|---|
| `docker/Dockerfile.init` | New — Alpine + AWS CLI v2 static binary, multi-arch via `uname -m` |
| `docker/init.version` | New — `1.0.0`, single source of truth for the image tag |
| `.github/workflows/build-init-image.yml` | New — builds/pushes to `ghcr.io/seekr-technologies/seekr-chain-init` on main push (paths filter) or `workflow_dispatch` |
| `src/seekr_chain/backends/argo/jobset.py` | Added `_INIT_IMAGE` constant (read from `user_config`); replaced 3 image keys with `"init_image": resolve_image(_INIT_IMAGE)` |
| `src/seekr_chain/backends/argo/templates/jobset.yaml.j2` | Replaced 3 init containers with single `init` container |
| `src/seekr_chain/user_config.py` | Added `SEEKRCHAIN_INIT_IMAGE` → `init_image` to `_ENV_VAR_MAP`; added `init_image` field to `UserConfig` |
| `src/seekr_chain/utils.py` | Fixed `resolve_image` to strip existing registry host before applying prefix |
| `tests/unit/test_manifest_rendering.py` | Updated `test_init_containers_present` assertion; added `test_init_container_has_required_env` |
| `tests/unit/test_image_prefix.py` | Added tests for registry-host-stripping behavior |
| `tests/unit/test_user_config.py` | Added `TestInitImageConfig` class covering env, .env, toml, and unset cases |
| `tests/hermetic/cluster.py` | Updated `HERMETIC_IMAGES` to reference new init image instead of the 3 old images |

### Key Decisions

- **Alpine base**: Alpine already has busybox and tar — only AWS CLI needs adding, keeping the image small.
- **`uname -m` detection**: Handles both `x86_64` (amd64) and `aarch64` (arm64) in a single Dockerfile layer, enabling multi-arch builds without QEMU awkwardness.
- **Version file vs inline constant**: `docker/init.version` is the CI-facing source of truth; the Python constant `_INIT_IMAGE` in `jobset.py` is the code-facing reference. Both are updated together when the image changes.
- **`:latest` guard**: The CI workflow only pushes `:latest` when running on `main`; `workflow_dispatch` from a branch pushes only the versioned tag.
- **`resolve_image()` fixed for GHCR images**: The new init image has a `ghcr.io/` registry host. Without a fix, applying `SEEKR_CHAIN_IMAGE_PREFIX` would produce a double-registry path like `registry.example.com/mirror/ghcr.io/seekr-technologies/...`. The fix strips the leading registry host (detected by a `.` or `:` in the first `/`-separated component) before prepending the prefix.
- **Init image override via full config stack**: Users can override the init image via `SEEKRCHAIN_INIT_IMAGE` using the same 4-layer precedence as `datastore_root` (env var → `.env` → `.seekrchain.toml` → `~/.seekrchain.toml`). This is useful when proxying through an internal registry without needing to set `SEEKR_CHAIN_IMAGE_PREFIX` globally.

### Sequencing Note for Future Agents

When bumping the init image (new AWS CLI version, etc.):
1. Update `docker/Dockerfile.init` and bump `docker/init.version`
2. Use `workflow_dispatch` on the feature branch to build and push the new tag
3. Update `_INIT_IMAGE` in `jobset.py` in the same PR
4. Merge — the build workflow on main will re-push idempotently
