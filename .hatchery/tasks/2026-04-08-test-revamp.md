# Task: test-revamp

**Status**: complete
**Branch**: hatchery/test-revamp
**Created**: 2026-04-08 08:48

## Objective

Revamp the testing infrastructure to achieve:
1. Clean, independent separation between unit and integration tests
2. Reduced integration test count by shifting coverage to faster unit tests

## Context

The root `tests/conftest.py` had an `autouse=True` fixture `patch_configs_for_testing` that depended on session-scoped `k3d_cluster` and `minio_service`. This meant that even `pytest tests/unit` would attempt to spin up a k3d cluster. Additionally, many integration tests duplicated coverage that could be provided at the unit level by inspecting generated manifests and asset files.

## Summary

### Key architectural decision: conftest scoping

pytest conftest files are only loaded for tests in their directory subtree. By moving all infrastructure fixtures into `tests/integration/conftest.py`, `pytest tests/unit` now runs in under 1 second with zero cluster/container dependencies.

### Files changed

| File | Change |
|------|--------|
| `tests/conftest.py` | Stripped to pytest hooks + `unique_test_name` only |
| `tests/integration/conftest.py` | **New** — all infrastructure fixtures: `_podman_socket`, `hermetic_flag`, `k3d_cluster`, `minio_service`, `gpu_image`, `datastore_root`, `test_id`, `test_name`, `test_code_dir`, `v1_api`, `s3_client`, `job_name`, `patch_configs_for_testing` |
| `tests/unit/test_validation.py` | **Moved** from `tests/integration/core/test_validation.py` |
| `tests/unit/test_manifest_rendering.py` | Expanded: `TestJobsetEnvAndConfig` class with env var merge and name truncation tests |
| `tests/unit/test_asset_generation.py` | **New** — 13 tests for `_construct_hostfile`, `_compute_peermap`, `_write_peermaps_and_scripts`, and `chain-entrypoint.sh` |
| `tests/integration/test_s3_utils.py` | **Moved** from `tests/unit/` — requires MinIO |
| `tests/integration/core/test_basic_job.py` | Removed ~10 tests now covered by unit tests; replaced with `test_full_workflow` + `test_failure_modes` |
| `tests/integration/lifecycle/test_logs.py` | Merged `test_basic` + `test_timestamps` → `test_basic_and_timestamps` |
| `.github/workflows/ci.yml` | Split `hermetic-tests` into `unit-tests` (no k3d, ~10min) and `integration-tests` (hermetic cluster, `needs: [lint, unit-tests]`) |
| `tests/pytest.ini` | Added `integration` marker |

### Gotchas for future agents

- **Single-role path normalization**: `build_jobset_context` sets `role_configs[0].name = ""` for `SingleRoleStepConfig`. When testing `_construct_hostfile` or `_write_peermaps_and_scripts` directly, you must do the same (`.model_copy()` then `.name = ""`). Otherwise `_generate_role_asset_path` appends `/role=<step_name>` instead of stopping at `step=<step_name>`.
- **`test_s3_utils.py` is an integration test**: it uses `minio_service` + `s3_client` and lives in `tests/integration/` despite its name.
- **CI gate order**: `integration-tests` job requires `unit-tests` to pass first. This ensures fast failures before the slow hermetic cluster spin-up.
- **`tests/pytest.ini` is the authoritative pytest config** (not a root `pytest.ini`). The `pythonpath = ..` line makes `import seekr_chain` resolve correctly when running from within `tests/`.
