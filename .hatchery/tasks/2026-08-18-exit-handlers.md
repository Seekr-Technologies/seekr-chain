# Task: exit-handlers

**Status**: proposed
**Branch**: hatchery/exit-handlers
**Created**: 2026-08-18

## Objective

Let each step declare one or more **exit handlers** — extra single-pod jobs the
controller runs after the step finishes, gated on `on_success` / `on_failure` /
`always`. A handler must be able to *see why the parent step ended* (exit code, OOM,
failure reason/message) and act on it (alert, upload, clean up). Handlers should be low
resource by default but configurable, and a handler's own outcome must never change the
step's or workflow's status.

## Context

The [pure-jobsets ADR](2026-04-21-pure-jobsets.md) explicitly called out that dropping
Argo unlocked separate `on_success` / `on_failure` hooks (Argo only had a single
`on_exit` that fired after *every* step, even successful ones, just to do nothing). That
capability was never built. This ADR builds it, on the k8s backend only.

Relevant current facts (from a code trace of this worktree):

- The controller (`resources/controller.py`) drives the DAG purely off each worker
  JobSet's `status.terminalState`; it never lists pods or reads container exit codes.
- A worker container exits with the user script's exit code (`chain-entrypoint.sh`:
  `exit "$rc_main"`); the code is not exported anywhere the controller can read except
  the pod's own container `terminated` state via the k8s API.
- Client-side, the numeric exit code is discarded — `workflow_state.py`
  `_populate_from_terminated` keeps only zero/nonzero and a special-cased `OOMKilled`
  reason.
- Log assets are keyed by an S3 prefix that `parse_logs.py` globs as
  `step=*/role=*/job_index=*/pod_index=*/attempt=*`.

Companion feature: [exit-code-retries](2026-08-18-exit-code-retries.md) (independent).

## Design

**A handler is a pseudo-step at the manifest/asset layer, not a DAG node.** It renders
through the *same* `jobset.py:build_jobset_context()` → `templates/jobset.yaml.j2` path
as a real step, so it inherits `chain-init`, the log-sidecar, PVCs, and affinity for
free, and lands in the assets tarball at `assets/step=<pseudo>/jobset.yaml` — a shape
`controller._load_manifest()` already understands. Crucially it is **absent from
`dag.json`**, which is what guarantees the controller's `_submit_ready_steps` /
`_cascade_fail` never see it: a handler's outcome can never cascade in the DAG or flip a
phase.

Decisions locked with the user: config is `exit_handlers: list[...]` per step, each
tagged `when`; handlers show as nested rows under the parent step in `status`/`follow`;
a handler failure is independent; k8s backend only (local backend gets a hard
`NotImplementedError` guard).

### Config — `src/seekr_chain/config.py`

```python
_DEFAULT_HANDLER_RESOURCES = ResourceConfig(
    num_nodes=1, cpus_per_node="500m", mem_per_node="512Mi",
    ephemeral_storage_per_node="10G", gpus_per_node=0, shm_size="64M",
)

class ExitHandlerConfig(RoleSpecConfig):
    when: Literal["on_success", "on_failure", "always"] = "always"
    on_exit_codes: list[int] | None = None          # optional extra gate (see below)
    resources: ResourceConfig = _DEFAULT_HANDLER_RESOURCES
```

Subclassing `RoleSpecConfig` (not a standalone model) means `_build_role_context`,
`_get_step_resources`, `_write_peermaps_and_scripts`, and the RFC1123-name /
image-xor-nix validators all apply unchanged. A `model_validator(mode="after")` rejects
`nix is not None` (nix closures aren't resolved for handlers), `resources.num_nodes != 1`,
and `depends_on is not None`; codes in `on_exit_codes` must be `0..255`.

Add `exit_handlers: list[ExitHandlerConfig] = []` to **both** `SingleRoleStepConfig` and
`MultiRoleStepConfig` — **not** to `RoleSpecConfig`. The controller's only trigger is a
step-level JobSet `terminalState`; there is no per-role terminal state to key a per-role
handler on (especially under an `ANY` success policy). Because `config.BaseModel` sets
`extra="forbid"`, leaving the field off `RoleSpecConfig` makes `roles[].exit_handlers` a
natural hard validation error for multi-role steps.

Add a pure `handler_step_name(step, handler) -> str` (e.g. `f"{step}-eh-{handler}"`) and
a `WorkflowConfig` validator `check_exit_handlers()` (sibling of `check_depends_on`):
handler names unique within a step, and no pseudo-name collision with any real step name
or another handler's pseudo name (hard error, not auto-suffixed).

**Rich context is the point (env vars).** The primary way a handler reacts is by reading
env vars the controller injects (see table below) and branching in-script.
`on_exit_codes` on a handler is an *optional* extra predicate — the handler fires when
the `when` clause matches **and** (`on_exit_codes` is `None` **or** the parent exit code
is listed) — near-free because the controller already reads the code, but secondary to
the env-var mechanism.

Local guard: in `backends/local/local_workflow.py`, before the step loop, raise
`NotImplementedError` if any step declares `exit_handlers`.

### Asset layout — new `src/seekr_chain/backends/k8s/exit_handlers.py`

```python
@dataclass(frozen=True)
class HandlerPlan:
    parent_step: str
    handler: ExitHandlerConfig
    pseudo_step: str   # config.handler_step_name(parent_step, handler.name)

def plan_handlers(config: WorkflowConfig) -> list[HandlerPlan]
```

Called once from `_package_assets()` so rendering and the handler index share names. New
`assets/handlers.json` — a flat list of `{parent, name, step, when, on_exit_codes}`.
`assets/dag.json` gets **no** handler entries.

Handler assets use `step=<pseudo>` (not a new `handler=` path segment) specifically
because `parse_logs.py` globs `step=*/role=*/job_index=*/pod_index=*/attempt=*` — a
`handler=` segment would not match and handler logs would silently vanish from
`chain logs`. With `step=<pseudo>` the handler's log prefix is structurally identical to
any single-role step's, so `chain logs` / `parse_logs.py` need zero changes.

### Client render — `src/seekr_chain/backends/k8s/jobset.py`

New `build_handler_jobset_context()` / `create_handler_jobset_manifest()` beside the step
versions: build a synthetic single-role `SingleRoleStepConfig` from the handler's fields
(name = pseudo_step), reuse `_write_peermaps_and_scripts`, `_build_role_context`,
`_build_affinity`, `_build_jobset_labels`, `_get_step_resources` verbatim; set
`success_policy=None`, `failure_policy={"maxRestarts": 0}` (a crashing handler must not
retry), `interactive=False`; add context keys `handler_of`, `handler_name`,
`handler_when`. Apply the existing 63-char JobSet-name shortening with a
handler-specific fallback suffix (`-h<NN>-js`) so it can't collide with a step's
`-s<NN>-js` fallback.

### Template — `src/seekr_chain/backends/k8s/templates/jobset.yaml.j2`

- Two additive label blocks (JobSet `metadata.labels` and pod `template.metadata.labels`):
  `seekr-chain/handler-of`, `seekr-chain/handler-name`, `seekr-chain/handler-when` when
  `handler_of` is set (default `None` on the regular step context, so no `is defined`
  guard). `seekr-chain/step` / `step-name` keep the **pseudo** name — this is what lets
  `_group_pods_by_step()` and `controller._load_manifest()` work unchanged.
- **Failure-message plumbing (shared benefit):** add
  `terminationMessagePolicy: FallbackToLogsOnError` to the `main` container. On failure
  this populates the container's `terminated.message` with the log tail, giving handlers
  a real failure message (not just a reason). No-op on success; harmless for existing
  steps.

### Packaging — `src/seekr_chain/backends/k8s/launch_k8s_workflow.py`

In `_package_assets()`: call `plan_handlers(config)`, add a second loop rendering each
handler via `create_handler_jobset_manifest()` to `assets/step=<pseudo>/jobset.yaml`, and
write `assets/handlers.json`. Leave `dag.json` and `_build_controller_jobset()` untouched.
No RBAC change — the controller SA already has `jobsets: create,get,list,watch,delete`,
`pods: list,get`, `configmaps: create,get,patch`.

### Controller — `src/seekr_chain/backends/k8s/resources/controller.py`

Still stdlib + `kubernetes` + `pyyaml` only. New functions:

- `_load_handlers(assets_path) -> dict[str, list[dict]]` — read `handlers.json` grouped by
  parent step; missing file → `{}` (compat with old assets).
- `_read_step_exit_info(k8s_v1, namespace, workflow_id, step_name) -> dict` — the one new
  k8s read: `list_namespaced_pod(label_selector=
  "seekr-chain/job-id=<id>,seekr-chain/step=<step>")`, then per pod read the `main`
  container's `state.terminated.exit_code` / `.reason` / `.message`, picking a
  representative pod (prefer nonzero exit, prefer `OOMKilled`, tie-break latest
  `finished_at`). Wrapped in `try/except Exception` → returns an all-empty dict on
  failure (pods GC'd, RBAC hiccup); never raises into the watch loop.
- `_handler_env(...)` / `_inject_handler_env(manifest, env_entries)` — append plain
  `{"name","value"}` entries to the `main` container only.
- `_submit_handlers_for_step(...)` — for each `PENDING` handler of a step: gate on `when`
  vs the step's phase (`SUCCEEDED`→on_success/always, `FAILED`→on_failure/always,
  `CANCELLED`→never→mark `SKIPPED`), then on `on_exit_codes` if set; read exit info once
  per step; load + env-inject + submit; same 409 / 429-5xx / other error triage as
  `_submit_ready_steps`. **Never touches `phases` or calls `_cascade_fail`.**
- `_load_handler_states` / `_save_handler_states` — same ConfigMap (`<workflow_id>-phases`)
  under a **separate data key `"handlers"`** so handler state never collides with the
  `"phases"` key. Restore `SUBMITTED` as-is (not reset to `PENDING`) to avoid double
  env-injection on controller restart; the 409 branch is the backstop.
- `_workflow_settled(dag, phases, handlers, handler_states)` — `all(phases terminal)` and
  no handler still PENDING-with-terminal-parent-or-SUBMITTED. Replaces the three raw
  `all(p in _TERMINAL_PHASES ...)` loop-condition checks in `main()`, so the controller
  does not exit (and `chain follow` does not report "done") while a handler is still
  running.
- Drain timeout `SEEKR_CHAIN_HANDLER_DRAIN_TIMEOUT` (default 3600s): if handlers are still
  outstanding this long after all steps go terminal, log/event a warning and break rather
  than hang forever.

Watch loop: add a handler branch keyed by `js_to_handler` (built at submit) checked
**before** the `js_to_step` lookup; on terminal/suspend, emit
`HandlerSucceeded`/`HandlerFailed`/`HandlerCancelled`, update `handler_states`, and
`continue` — this `continue` keeps handler outcomes out of `_cascade_fail`/`phases`.
Handler JobSets already carry `seekr-chain/job-id`, so they stream through the existing
watch selector unchanged. Call `_submit_handlers_for_step` (a) once for already-terminal
restored phases before opening the watch (restart-safety) and (b) right after each step
phase transition. The final `main()` block (which computes `failed`/`cancelled` from
`phases` only and always returns 0) is untouched — add one informational log/event
listing failed handlers, but do not feed it into `WorkflowFailed`.

### Env vars injected into a handler pod

| Name | Source |
|---|---|
| `SEEKR_CHAIN_HANDLER_NAME` / `_WHEN` | `handlers.json` entry |
| `SEEKR_CHAIN_PARENT_STEP` / `_JOBSET` | parent step / JobSet name |
| `SEEKR_CHAIN_PARENT_STATUS` | `phases[step]` at dispatch (`SUCCEEDED`\|`FAILED`) |
| `SEEKR_CHAIN_PARENT_EXIT_CODE` | representative pod's `main` exit code, `""` if unknown |
| `SEEKR_CHAIN_PARENT_FAILURE_REASON` | terminated `.reason` (`OOMKilled`/`Error`/…), `""` |
| `SEEKR_CHAIN_PARENT_FAILURE_MESSAGE` | terminated `.message` (log tail via `FallbackToLogsOnError`), `""` |
| `SEEKR_CHAIN_PARENT_OOM_KILLED` | `"true"`/`"false"` |
| `SEEKR_CHAIN_PARENT_POD` / `_ROLE` | representative pod name / its `seekr-chain/role` |
| `SEEKR_CHAIN_PARENT_POD_EXITS` | `json.dumps([...per-pod exit info...])` |

Plus everything a normal step gets from `_get_env()` (workflow env, secrets,
`SEEKR_CHAIN_WORKFLOW_ID`, …).

### Client state & rendering

- `workflow_state.py`: new `HandlerState` dataclass (`name, when, parent, roles, pod,
  dt_start, dt_end`); `StepState` gains `handlers: list[HandlerState]`. Partition JobSets
  by presence of the `seekr-chain/handler-of` label so handler JobSets aren't counted in
  `WorkflowState.steps` (keeps the `N/T` count step-only), then attach each to its parent
  step reusing the existing pod/role collection helpers. A `SKIPPED` handler has no
  JobSet → no row.
- `render_status.py`: new `_handler_rows()` emitting one row per handler at pod
  indentation (`"<name> (<when>)"`, its single pod collapsed onto the row like a
  single-pod step), called from `_collect_rows()` after a step's pod rows; adjust the
  tree-glyph logic so a step's last pod uses `├` when handler rows follow.
- `watched_state.py`: **no changes** — its four `WatchSpec`s already select on
  `seekr-chain/job-id`.
- `k8s_workflow.py`: extend `follow()` to also walk `step_state.handlers` (reuse
  `_spawn_follow_pod_thread`). `get_logs()` / `cancel()` / `delete()` / `attach()` need no
  change (prefix download, label-based suspend/delete, and steps-only iteration already
  cover or correctly exclude handlers).

## Out of scope

- Local backend support (hard `NotImplementedError` guard only).
- Nix-mode handlers (rejected at validation).
- Per-role handlers on multi-role steps; workflow-level ("whole DAG") handlers.
- Handler retries (`maxRestarts: 0` fixed; no `failure_policy` field on a handler).
- `always` firing on `CANCELLED` — it does not; cancellation skips all handlers.

## Verification plan

Unit tests (run with
`PYTHONPATH=<venv>/site-packages:src /usr/bin/python3 -m pytest --noconftest tests/unit/...`
— `--noconftest` skips the root conftest that imports boto3/kubernetes and spins up k3d):

- `test_config.py`: `ExitHandlerConfig` when/defaults, nix rejection, `num_nodes`
  rejection, name-collision; `roles[].exit_handlers` rejected via `extra="forbid"`.
- `test_manifest_rendering.py`: handler manifest shape (labels, `maxRestarts: 0`, no
  successPolicy, default + overridden resources, log-sidecar prefix under `step=<pseudo>`,
  `terminationMessagePolicy` on `main`); a plain step manifest unaffected by the new
  template blocks.
- `test_asset_generation.py`: handler asset paths / `handlers.json` shape; `dag.json` has
  no handler entries.
- `test_controller.py` (new `TestHandlerDispatch`, reusing `_load_controller` /
  `_make_event` / `_run_main`): on_success/on_failure/always + `on_exit_codes` gating; no
  cascade from a failed handler in a diamond DAG; exit-code/OOM/message env injection from
  mocked pod statuses; missing exit info degrades gracefully; controller doesn't return
  until a handler settles; drain timeout; restart idempotency (409 + pre-seeded
  `SUBMITTED`); `CANCELLED` skips `always`; old assets without `handlers.json` behave as
  today.
- `test_build_workflow_state.py` / `test_status_rendering.py`: handler nesting under a
  parent step and row rendering.

Integration (`--real-cluster`, patterned on `tests/integration/lifecycle/test_logs.py`):
a step that exits 42 with an `on_failure` handler asserting
`SEEKR_CHAIN_PARENT_EXIT_CODE=42` in its logs and an `on_success` handler that must not
run; workflow status reflects the step, not the handler; a downstream step still runs. A
second case: a successful step + an `always` handler that itself fails → workflow still
`SUCCEEDED`.

Manual: `chain submit` an example config with an `on_failure` handler; `chain status` /
`chain follow` show it nested under the step; `chain logs` includes its output.

## References

- [exit-code-retries ADR](2026-08-18-exit-code-retries.md) — the companion feature.
- [pure-jobsets ADR](2026-04-21-pure-jobsets.md) — where separate success/failure hooks
  were pre-authorized; controller/asset architecture this builds on.
