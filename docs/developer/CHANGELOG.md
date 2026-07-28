# CHANGELOG
 
## v0.5.0 [unreleased]
- Added: Centralized kubeconfig loading in `k8s_utils.py` — friendly error when kubeconfig is missing or invalid.
- Added: `depends_on` validation in `WorkflowConfig` — references to non-existent step names now raise a clear `ValidationError`.
- Added: S3 credential error wrapping — missing AWS credentials produce actionable guidance instead of raw tracebacks.
- Added: Pre-flight check for `kubectl` in `attach()` — clear error with install link when `kubectl` is not in PATH.
- Fixed: Replaced stray `print()` calls with `logger` in `k8s_utils.py` and `launch_argo_workflow.py`.
- Fixed: Flaky integration tests caused by fluent-bit log sidecar buffering all logs until
  the 60-second upload timeout. `LoggingConfig.upload_timeout` is now wired to the sidecar's
  `FB_UPLOAD_TIMEOUT` env var, and hermetic tests use a 5-second timeout so logs flush
  well within the shutdown grace period.
- Changed: CI pipeline overhauled to use conventional commits for automatic releases.
  Every MR merged into `main` triggers an automatic release. Version bump is determined
  by scanning all commits in the MR: `feat!:` → major, `feat:` → minor, `fix:`/`chore:`/etc. → patch,
  `no-bump:` MR title → skip. Falls back to MR title if no commits use conventional format.
- Changed: Release pipeline no longer requires RC branches. Tags are created automatically
  by `create-release` CI job on push to `main`.
- Added: `preview-release` CI job posts (and updates) an MR comment showing the next version,
  bump type, and a changelog preview of all conventional commits in the MR.
- Added: `warn-major-bump` CI job (allow_failure) that fails visibly when the MR would
  trigger a major version bump, making breaking changes impossible to miss.
- Added: Package is now published to public PyPI on every release.
- Removed: `enforce_rc_branch.py` and `tag_branch.py` scripts (replaced by new CI jobs).
- Added: `chain status <JOB_ID>` — show workflow status and per-step/role/pod detail
- Added: `chain attach <JOB_ID>` — attach to an interactive workflow
- Added: `chain wait <JOB_ID>` — wait for workflow completion (exit code 1 on failure), supports `--poll-interval`
- Added: `chain list` — list workflows as a Rich table, supports `--namespace` and `--limit`
- Added: `--namespace` / `-n` option to `chain submit` to override config namespace
- Added: `--follow` / `-f` and `--all-replicas` flags to `chain logs` for live log tailing
- Added: `list_workflows()` public API function
- Added: `chain list` now shows "Job Name" and "User" columns, with `--user` filter option
- Added: `seekr-chain/job-name` and `seekr-chain/user` labels on workflow manifests at submit time
- Added: `ArgoWorkflow.get_detailed_state()` and `ArgoWorkflow.format_state()` public methods
- Added: Hermetic test environment — local k3d cluster + MinIO, no cloud credentials needed.
  Hermetic mode is the default; use `--real-cluster` or `--gpu` to opt out.
- Added: GitLab CI hermetic job (podman-based) and GitHub Actions workflow.
- Added: `scripts/install-argo.sh` — portable argo CLI installer.
- Added: `chain delete` CLI command and `ArgoWorkflow.delete()` method.
- Added: `ArgoWorkflow.delete()` method to delete a workflow from the cluster via the K8s API
- Added: `chain delete <JOB_ID>` CLI command
- Added: CLI unit tests (`tests/test_cli.py`) covering `submit`, `logs`, and `delete` commands using `CliRunner`
- Changed: Workflow submission now uses the K8s custom objects API directly; `argo` CLI binary is no longer required
- Changed: Interactive-mode disconnection hint updated from `argo delete` to `chain delete`
- BREAKING: K8s label keys migrated from `seekr-chain.<key>` (dot, underscore) to
  `seekr-chain/<key>` (slash, hyphen) to conform to the Kubernetes vendor-namespaced
  label standard. Existing workflows queried by old label selectors will not be found.
- BREAKING: `ArgoWorkflow.__init__` signature changed — `name` and `datastore_root`
  parameters removed. Pass only `id` (and optionally `namespace`).
- Added: `seekr-chain/datastore-root` workflow annotation stored at submit time,
  allowing `ArgoWorkflow(id=...)` to self-recover job paths without caller-supplied
  datastore root.
- Changed: `namespace` in `ArgoWorkflow` defaults to the kubeconfig active-context
  namespace when not specified (mirrors `kubectl` with no `-n` flag).

## v0.4.0 [26-02-03]
- BREAKING: These changes require config migration!
    - Switch role config from `command/args` -> `shell/script`
    - create dedicated `code` config, contains path, include, exclude
- Added:
    - Config:
        - Add `failure_policy` - allows for configurable restarts
        - Add `ephemeral_storage_per_node` -
            - Deafult `AUTO` mode requests 80% of storage on a node
    - Automatically mount `emptyDir` to `/tmp`. This makes all ephemeral storage
    available to /tmp (as well as backing the default `/seekr-chain` directory)
- Fixed:
    - Fix incorrect handling of code inclusion/exclusion rules

## v0.3.0 [25-12-01]
- Added
    - Add `HOSTFILE` for deepspeed support (with `--no_ssh` option)
    - `s3_utils`:
        - `sync` - Sync files/directories
        - `S3Cache` - Manage and sync files/directories automatic cache size management
    - `launch_argo_workflow`: add `args`

## v0.2.0 [25-08-26]
- Added
    - Support for multiple steps
    - `env` config option
    - Resolve symlinks when uploading code
    - Add support for `interactive` jobs
    - Add `security` options, required for high-bandwidth multinode jobs
    - Add Interactive jobs!
    - Add shm_size option
    - Add more default evars

- Fix
    - Automatically shorten JS names if they exceede k8s limits
    - Fix issue with CLI installation

## v0.1.0 [25-06-13]
- Initial release

