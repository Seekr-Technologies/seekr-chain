# Task: fix-invalid-job-name

**Status**: in-progress
**Branch**: hatchery/fix-invalid-job-name
**Created**: 2026-07-14 15:24

## Objective

Background

  Production incident: a workflow with DAG step name vmf-n0-1_v2 failed. Confusingly, the visible log output was:

  [controller] loaded DAG with 1 steps: ['vmf-n0-1_v2']
  [controller] restored phases from ConfigMap: ['vmf-n0-1_v2']
  [controller] warning: could not emit event 'WorkflowFailed': (403) Forbidden — events is forbidden: User "system:serviceaccount:argo-workflows:argo-workflow" cannot create
  resource "events"...

  This looked like an RBAC problem, but it's a red herring. We confirmed via kubectl auth can-i that the argo-workflow ServiceAccount genuinely lacks create events, but can create
  jobsets and create/patch configmaps. _emit_event in src/seekr_chain/backends/k8s/resources/controller.py already catches that 403 in a bare except Exception and only logs a
  warning — it never raises, so it did not cause the failure.

  Actual root cause

  The DAG step name vmf-n0-1_v2 contains an underscore. Kubernetes resource names must be RFC 1123 label-compliant: lowercase alphanumeric characters and - only, must start/end
  with an alphanumeric character. No validation anywhere in this repo enforces that.

  Flow that breaks:
  1. build_jobset_context() in src/seekr_chain/backends/k8s/jobset.py:508 does js_name = f"{workflow_name}-{step_name}-js" — the step name is embedded verbatim into the JobSet's
  metadata.name, with zero sanitization (the only existing transform on js_name is a length-truncation fallback a few lines below, nothing character-related).
  2. The k8s API rejects the invalid name at JobSet creation (400/422).
  3. _submit_ready_steps in src/seekr_chain/backends/k8s/resources/controller.py (lines ~254-263) correctly classifies this as a permanent error (as opposed to retriable
  409/429/5xx) and marks the step FAILED.
  4. That FAILED state gets persisted to the phases ConfigMap (_save_phases). Every subsequent controller pod restart immediately reads FAILED back out of the ConfigMap and exits 1  within ~1 second — which is why kubectl describe pod showed the controller container terminating almost instantly with no real diagnostic output.

  src/seekr_chain/config.py currently has RoleSpecConfig.name, MultiRoleStepConfig.name, and WorkflowConfig.name all as plain str fields with no charset validation.
  WorkflowConfig's docstring even claims name "must be DNS-compliant" but nothing enforces it.

  Fix

  Reject invalid names at config-parse time (fail fast, with a clear error) instead of letting them fail silently at k8s submission time.

  1. In src/seekr_chain/config.py, add a module-level RFC 1123 label regex (e.g. ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$) and a small _validate_rfc1123_name(name: str) -> str helper that
  raises ValueError with a message explaining the constraint (no underscores/uppercase, must start/end alphanumeric) when a name doesn't match.
  2. Add @field_validator("name") (pydantic, @classmethod, calling the shared helper) to:
    - RoleSpecConfig — covers role names; inherited automatically by SingleRoleStepConfig since it subclasses RoleSpecConfig.
    - MultiRoleStepConfig
    - WorkflowConfig

  This fires during WorkflowConfig.model_validate(), the common entry point used by src/seekr_chain/cli.py:62, src/seekr_chain/backends/k8s/launch_k8s_workflow.py:404, and
  src/seekr_chain/backends/local/local_workflow.py:121 — so the check applies for both k8s and local backends without needing separate hooks.
    - Follow the existing validator pattern already in the file (see SingleRoleStepConfig.check_failure_policy, MultiRoleStepConfig.check_failure_policy,
  WorkflowConfig.check_depends_on — all @pydantic.model_validator(mode="after") raising ValueError with a descriptive message).
  3. Add tests to tests/unit/test_validation.py (new test class alongside the existing TestValidationFailurePolicy), covering: valid names pass; underscore, uppercase,
  leading/trailing hyphen, and empty-string names all raise ValueError, for each of: single-role step name, multi-role step name, role name within a multi-role step, and workflow
  name.
  4. Run pytest tests/unit/test_validation.py (and the broader unit suite) to confirm.

  Explicitly out of scope

  The operate-workflow-role ClusterRole / missing events:create RBAC gap is external cluster infra, not managed in this repo — do not touch RBAC manifests as part of this fix.
  _emit_event's existing best-effort error handling is correct as-is and needs no change.

## Agreed Plan

*(To be filled in after planning discussion)*

## Progress Log

*(Steps will appear here once the plan is agreed)*

## Summary

*(Fill in on completion — then remove Agreed Plan and Progress Log above.
Cover: key decisions made, patterns established, files changed, gotchas,
and anything a future agent working in this repo should know.)*
