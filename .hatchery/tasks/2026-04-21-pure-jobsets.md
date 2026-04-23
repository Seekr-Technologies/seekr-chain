# Task: pure-jobsets

**Status**: complete
**Branch**: hatchery/pure-jobsets
**Created**: 2026-04-21 18:39

## Objective

Remove Argo Workflows entirely and replace with a pure-JobSet "k8s" backend.
The new backend submits a small controller `batch/v1 Job` that downloads assets
from S3 (same init-container pattern as workers), then runs `resources/controller.py`
to execute the DAG by submitting and monitoring worker JobSets directly.

## Context

seekr-chain previously used Argo Workflows as a DAG coordinator: each step was an Argo
`resource` task that created a JobSet and polled for `terminalState`. Argo was a heavy
dependency for what is essentially a simple "wait-for-deps → submit JobSet → poll" loop.

Key design constraint: the controller pod must run a **slim Python image** (no seekr_chain
installed) — so all JobSet manifests are pre-rendered **client-side** and packaged into
the S3 asset tarball. The controller only needs `kubernetes` + `pyyaml`.

## Summary

### Architecture

```
Client (chain submit):
  1. Package assets → tarball:
       assets/dag.json               — DAG structure [{name, depends_on}]
       assets/step=<name>/jobset.yaml — pre-rendered JobSet manifests (1 per step)
       resources/controller.py       — DAG runner script
       (+ existing scripts/peermaps/hostfiles/entrypoint/fluentbit)
  2. Upload to S3, create K8s Secret, ensure RBAC (idempotent)
  3. Submit controller batch/v1 Job to K8s

Controller Job (batch/v1, backoffLimit=0):
  Init containers: download-assets (awscli image), unpack-assets (alpine)
  Main container: config.controller_image → python /seekr-chain/resources/controller.py
  Env: SEEKR_CHAIN_NAMESPACE, SEEKR_CHAIN_JOB_ASSET_PATH,
       SEEKR_CHAIN_CONTROLLER_JOB_NAME (downward API from batch.kubernetes.io/job-name)

controller.py (stdlib + kubernetes + yaml only):
  - Reads assets/dag.json and per-step jobset.yaml from disk
  - Sets ownerReference on each JobSet → cascade-deleted with controller Job
  - Polls JobSet terminalState (Completed/Failed), cascade-fails downstream steps
  - Exits 0 on success, 1 on any failure

Worker JobSets: same as before (submitted by controller, not Argo)
```

### Key decisions

**`assets_path` made optional in `jobset.py`**: `build_jobset_context`,
`_build_role_context`, and `create_jobset_manifest` now accept `assets_path: Path | None`.
When `None`, the file-writing side-effects (peermaps, hostfiles, scripts) are skipped —
used when pre-rendering manifests for the tarball without writing to disk.

**`_synthetic_step_pod()`**: The Argo model had a dedicated "step pod" alongside role pods.
`K8sWorkflow.get_detailed_state()` now synthesizes a virtual `PodState` from aggregate role
statuses to maintain compatibility with `format_state()` rendering.

**RBAC**: `ensure_rbac(namespace)` is called idempotently on every `launch_k8s_workflow()`.
The `seekr-chain-controller` ServiceAccount needs: `jobset.x-k8s.io/jobsets`
(create/get/list/watch/delete), `batch/jobs` (get — to read own UID for ownerReferences),
pods (list/get — for `get_detailed_state()`).

**`controller_image`** is a required field in practice (no default) — users must specify
a slim Python image with `kubernetes` and `pyyaml`. Field is `Optional[str] = None` in
the model; `launch_k8s_workflow` raises if it's `None` at submission time.

### Backward compatibility

- `from seekr_chain.backends.argo import ArgoWorkflow` → `DeprecationWarning` → `K8sWorkflow`
- `seekr_chain.ArgoWorkflow` → `DeprecationWarning` → `K8sWorkflow`
- `seekr_chain.launch_argo_workflow` → `DeprecationWarning` → `launch_k8s_workflow`
- `Backend.ARGO` → enum alias for `Backend.K8S` (no warning; enum aliasing is silent)
- `backends/argo/__init__.py` kept as a compat shim; all other argo submodules deleted

### Files changed

| Action | Path |
|--------|------|
| Delete | `backends/argo/argo_workflow.py` |
| Delete | `backends/argo/launch_argo_workflow.py` |
| Delete | `backends/argo/templates/workflow.yaml.j2` |
| Delete | `backends/argo/list_workflows.py` |
| Delete | `backends/argo/{job_info,jobset,parse_logs,render}.py` |
| Delete | `backends/argo/resources/`, `backends/argo/templates/` (except `__init__.py`) |
| Replace | `backends/argo/__init__.py` → compat shim |
| Create | `backends/k8s/__init__.py` |
| Create | `backends/k8s/launch_k8s_workflow.py` |
| Create | `backends/k8s/k8s_workflow.py` |
| Create | `backends/k8s/list_workflows.py` |
| Create | `backends/k8s/rbac.py` |
| Create | `backends/k8s/jobset.py` (from argo + optional assets_path) |
| Create | `backends/k8s/{job_info,parse_logs,render}.py` (from argo, unchanged) |
| Create | `backends/k8s/templates/` (from argo) |
| Create | `backends/k8s/resources/` (from argo + new controller.py) |
| Modify | `workflow.py` (Backend enum: K8S + ARGO alias) |
| Modify | `config.py` (add `controller_image`) |
| Modify | `__init__.py` (wire k8s, compat aliases via `__getattr__`) |
| Modify | `cli.py` (ArgoWorkflow → K8sWorkflow) |
| Modify | `print_logs.py` (ArgoWorkflow → K8sWorkflow) |
| Modify | `tests/conftest.py` (`launch_argo_workflow` → `launch_k8s_workflow`) |
| Modify | All test files (ArgoWorkflow → K8sWorkflow, launch_argo → launch_k8s) |

### Gotchas for future agents

- The unit test suite requires docker/podman (hermetic k3d cluster). This is a pre-existing
  constraint — tests cannot run in this sandbox environment.
- `SEEKR_CHAIN_CONTROLLER_JOB_NAME` is injected via the K8s downward API
  (`batch.kubernetes.io/job-name` label on the pod), not as a literal value. This requires
  the controller pod's serviceAccount to have `batch/jobs: get`.
- controller.py stores manifest names in `submitted_names: dict[str, str]` keyed by step
  name to avoid re-reading YAML on every poll iteration.
