# Task: exit-hammer

**Status**: complete
**Branch**: hatchery/exit-hammer
**Created**: 2026-04-27 15:34

## Objective

Add per-step and workflow-level exit handler support to seekr-chain, mirroring Metaflow's
`@exit_hook` decorator. A step's exit handler always runs after that step terminates —
whether it succeeded, failed, or was killed. A workflow-level exit handler always runs
after the entire workflow terminates.

## Context

Metaflow's `@exit_hook` is applied as a decorator *on a step*, causing a
cleanup/notification step to run whenever that step terminates. This is more reliable
than downstream DAG steps, which are skipped when an upstream step fails.

seekr-chain previously had `after_script`, which runs inline within a step's own pod and
is skipped entirely when the main script is OOM-killed or preempted. There was no
guaranteed post-step cleanup mechanism.

Argo Workflows supports this natively at the **DAG task level** via `onExit: <template-name>`
on individual tasks. The exit template runs when that task reaches any terminal state.

## Summary

### API

Any step can now declare an `on_exit` block:

```yaml
steps:
  - name: train
    image: pytorch:2.0
    script: python train.py
    on_exit:
      image: python:3.11
      script: |
        echo "train finished with status: $STEP_STATUS"
        python notify.py
      resources:
        cpus_per_node: 1
        mem_per_node: "4G"
        ephemeral_storage_per_node: "10G"
```

`STEP_STATUS` is injected automatically — `"Succeeded"` or `"Failed"` (local backend) or
the Argo template literal `{{tasks.<step>.status}}` (Argo backend, rendered by Argo when
creating the exit JobSet resource).

A workflow-level exit handler can also be declared at the top level:

```yaml
on_exit:
  image: python:3.11
  script: |
    echo "workflow finished with status: $WORKFLOW_STATUS"
    python send_notification.py
```

`WORKFLOW_STATUS` is injected automatically — `"Succeeded"` or `"Failed"` (local backend)
or `{{workflow.status}}` (Argo backend, rendered via `spec.onExit`). Per-step and
workflow-level exit handlers are fully independent and can both be defined.

### Key Design Decisions

1. **Two levels**: per-step `on_exit` (keyed by `STEP_STATUS`) and workflow-level `on_exit`
   (keyed by `WORKFLOW_STATUS`). Both are optional and can coexist.

2. **`ExitStepConfig` is a separate model** from `StepConfig` — it has no `depends_on`
   or `failure_policy` (those are meaningless for exit handlers), making the schema
   self-documenting.

3. **Argo `onExit` at the DAG task level**: the exit step is a full JobSet template that
   Argo runs after the parent task completes. This guarantees execution even if the main
   pod was OOM-killed, since the exit step runs in a completely separate pod.

4. **`STEP_STATUS` as an Argo template variable**: `{{tasks.<step>.status}}` is passed
   as the env var value in the exit step's JobSet YAML. Jinja2 does not re-evaluate the
   output of `{{ variable }}` expressions, so the literal passes through unchanged and
   Argo renders it when creating the resource.

5. **`build_jobset_context` refactored** to accept `step_config` directly (in addition
   to `step_index`), so exit steps can be built without appending them to
   `workflow_config.steps`.

### Files Changed

| File | Change |
|------|--------|
| `src/seekr_chain/config.py` | `ExitStepConfig` model; `on_exit` field on `SingleRoleStepConfig` and `MultiRoleStepConfig` |
| `src/seekr_chain/__init__.py` | Export `ExitStepConfig` |
| `src/seekr_chain/backends/argo/jobset.py` | `build_jobset_context` / `create_jobset_manifest` accept optional `step_config` parameter |
| `src/seekr_chain/backends/argo/launch_argo_workflow.py` | `_exit_step_name`, `_create_exit_step_manifest`, `_create_workflow_exit_step_manifest`, updated `_create_dag_task` and `_create_workflow_manifest` |
| `src/seekr_chain/backends/argo/templates/workflow.yaml.j2` | DAG task `onExit` field + exit step templates section + `spec.onExit` + workflow exit template |
| `src/seekr_chain/backends/local/local_workflow.py` | Per-step exit after each step; workflow exit after all steps |
| `tests/unit/test_config.py` | `TestExitStepConfig` + `TestWorkflowOnExit` classes |
| `tests/unit/test_manifest_rendering.py` | `TestExitStepJobsetRendering`, `TestWorkflowExitJobsetRendering`, workflow template exit tests; `test_init_containers_present` in both exit rendering classes |
| `tests/unit/test_local_execution.py` | `TestExitHandler` + `TestWorkflowExitHandler` classes |
| `tests/integration/core/test_basic_job.py` | `TestExitStepAssets` class with `test_step_exit_assets` and `test_workflow_exit_assets` |
| `docs/guides/exit-handlers.md` | New guide covering step-level and workflow-level exit handlers |
| `docs/features.md` | Exit handlers bullet |
| `docs/reference/environment-variables.md` | `STEP_STATUS` and `WORKFLOW_STATUS` entries |

### Gotchas for Future Agents

- The unit tests in this repo require kubernetes tooling (docker, k3d, kubectl, argo)
  even for tests that don't use a cluster — the root `tests/conftest.py` has
  `autouse=True` fixtures that attempt cluster setup. This is a sandbox limitation;
  the test logic is verified via direct Python imports/calls.
- `ExitStepConfig.env` does NOT include `STEP_STATUS`/`WORKFLOW_STATUS` — they are always
  injected automatically (overriding any user-provided value). This is intentional.
- Per-step exit step names are derived as `{parent_step_name}-exit`; there is no validation
  that this derived name doesn't clash with another user step name. If a user has a step
  named `"train-exit"`, the Argo template names will collide. This edge case was not addressed.
- Workflow exit uses reserved Argo template name `"seekr-chain-workflow-exit"` and internal
  name `"__workflow-exit__"` to avoid collisions with user step names.
