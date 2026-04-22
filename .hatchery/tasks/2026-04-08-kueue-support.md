# Task: kueue-support

**Status**: complete
**Branch**: hatchery/kueue-support
**Created**: 2026-04-08 16:48

## Objective

Add support for `kueue` to our jobs.

## Context

Kueue is a Kubernetes-native job queuing system that integrates with JobSet
via labels on the JobSet metadata (`kueue.x-k8s.io/queue-name`,
`kueue.x-k8s.io/priority-class`). The question was whether to add a Kueue-
specific field or a more general labels mechanism.

## Summary

### Key decision: backend-agnostic `scheduling` block

Rather than a `kueue`-specific config (which would tie the user-facing API
to Kubernetes) or a raw labels passthrough, we added a `SchedulingConfig`
model with abstract fields:

- `queue: str` — queue/partition name
- `priority: Optional[str]` — priority class / QOS name

The Argo/k8s backend maps these to Kueue labels. A future SLURM backend
would map them to `--partition` / `--qos` without any config schema changes.

### Files changed

| File | Change |
|------|--------|
| `src/seekr_chain/config.py` | Added `SchedulingConfig`; added `scheduling: Optional[SchedulingConfig] = None` to `WorkflowConfig` |
| `src/seekr_chain/backends/argo/jobset.py` | Added `_build_scheduling_labels()`; passed `scheduling_labels` into `build_jobset_context()` context |
| `src/seekr_chain/backends/argo/templates/jobset.yaml.j2` | Emits kueue labels in `metadata.labels` when `scheduling_labels` is set |
| `tests/unit/test_manifest_rendering.py` | 3 new tests covering: no labels by default, queue label, queue+priority labels |

### Usage

```yaml
name: my-training-job
scheduling:
  queue: gpu-queue
  priority: high    # optional
steps:
  - name: train
    image: pytorch:2.0
    script: python train.py
```

### Patterns established

- Backend-specific label/flag mappings live in the backend module
  (`jobset.py`), not in the shared config. The config stays backend-agnostic.
- The `scheduling` block follows the same pattern as `affinity` (workflow-level,
  applies to all steps/JobSets).
- A `kubernetes`-specific config section was intentionally deferred — add it
  when there is a concrete need for k8s-only overrides.
