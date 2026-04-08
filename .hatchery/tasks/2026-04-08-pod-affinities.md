# Task: pod-affinities

**Status**: complete
**Branch**: hatchery/pod-affinities
**Created**: 2026-04-08 08:45

## Objective

Replace the flat `AffinityConfig` dict with a typed, extensible list of affinity rules that maps cleanly onto Kubernetes primitives — enabling node selection, pod co-location (packing), and pod spreading (anti-affinity) from a single unified config field.

## Context

The original `AffinityConfig` had `nodes`, `labels`, and `pack` as sibling keys — mixing node-selection concerns with pod-to-pod scheduling, and providing no path to anti-affinity or multiple rules of the same kind. The replacement is a discriminated list of `AffinityRule` objects, each typed as either `node` or `pod`, with a `direction` of `attract` or `repel` and a `required` boolean. This maps directly to the six Kubernetes affinity sub-trees: `nodeAffinity` required/preferred, `podAffinity` required/preferred, `podAntiAffinity` required/preferred.

The old dict format is preserved via a `field_validator(mode="before")` coercion on `WorkflowConfig` so existing configs continue to work without changes.

## Summary

Restructured `WorkflowConfig.affinity` from an opaque `AffinityConfig` dict to `Optional[list[AffinityRule]]`, where `AffinityRule` is a Pydantic discriminated union:

```yaml
affinity:
  - type: node
    direction: attract          # attract (default) or repel
    hostnames: [gpu-node-01]
    required: true              # true (default for node rules) = hard constraint

  - type: node
    direction: repel
    labels: {maintenance: ["true"]}
    required: false             # false = soft preference

  - type: pod
    direction: attract          # co-locate with pods in this group
    group: experiment-42
    required: false             # false (default for pod rules) — avoid deadlock

  - type: pod
    direction: repel            # spread away from pods in this group
    group: inference-prod
    required: true
```

**Key decisions:**

- `type: Literal["node", "pod"]` is the Pydantic discriminator; `direction: Literal["attract", "repel"]` replaces the K8s-leaking `affinity`/`anti-affinity` split.
- `required` defaults differ by type: `True` for node rules (was always hard before), `False` for pod rules — because `required=True` pod attract deadlocks on a fresh submission when no pods with the group are yet running. A `model_validator` emits a `UserWarning` when this dangerous combination is used.
- `topology_key` is hardcoded to `kubernetes.io/hostname` — not exposed. The only meaningful value for our use-cases is same-node, and exposing it would complicate a future Slurm backend with no current benefit.
- Multiple attract-groups are supported: each pod carries one label per attract group (`seekr-chain/pg.{group}: "true"`), and the affinity selector matches that specific label key. A pod can satisfy multiple groups simultaneously.
- `pack_group` (singular) context var renamed to `pack_groups` (list) in the Jinja2 template context.
- Jinja2 template uses `'key' in dict` guards (not `dict.key`) throughout the affinity block because the environment uses `StrictUndefined` — dot-access on a missing key raises `UndefinedError`.

**Backward compatibility:** Old dict-shaped affinity configs are transparently coerced to the new list format by `WorkflowConfig._coerce_legacy_affinity`. No existing user configs break.

**Files changed:**
- `src/seekr_chain/config.py` — removed `AffinityConfig`; added `NodeAffinityRule`, `PodAffinityRule`, `AffinityRule` union; changed `WorkflowConfig.affinity` type; added backward-compat coercion validator
- `src/seekr_chain/backends/argo/jobset.py` — rewrote `_build_affinity()` to bucket rules into six K8s affinity sub-trees; `pack_group` → `pack_groups` list
- `src/seekr_chain/backends/argo/templates/jobset.yaml.j2` — pod labels loop over `pack_groups`; full affinity block with `nodeAffinity` (required + preferred), `podAffinity`, and `podAntiAffinity`
- `tests/unit/test_manifest_rendering.py` — `TestAffinityRendering` class with 20 tests covering all rendering paths: node attract/repel with hostnames/labels, required/preferred, pod attract/repel soft/hard, label emission, multiple groups, backward compat coercion, deadlock warning
- `tests/integration/core/test_basic_job.py` — `TestAffinity` and `TestPacking` updated to new list format
