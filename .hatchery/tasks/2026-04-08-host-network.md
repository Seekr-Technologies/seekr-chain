# Task: host-network

**Status**: complete
**Branch**: hatchery/host-network
**Created**: 2026-04-08 08:36

## Objective

Make `host_network` configurable per step to avoid port conflicts on shared nodes while preserving InfiniBand/RDMA performance for multi-node jobs.

## Context

`hostNetwork: true` was hard-coded for all pods. This is required for multi-node distributed training so NCCL can reach InfiniBand/RoCE devices (without `hostNetwork`, IB interfaces are not visible in the pod's network namespace, and NCCL falls back to TCP). However, single-node jobs that request fewer than all GPUs on a node can share a node with other jobs, causing port conflicts because all pods with `hostNetwork: true` compete for the same host port space (e.g. port 29500 for PyTorch rendez-vous).

Disabling `hostNetwork` globally was ruled out since the cluster does not have SR-IOV or an RDMA device plugin installed — those would be the only way to get IB access without host networking.

## Summary

Added `host_network: bool | Literal['AUTO'] = 'AUTO'` to `ResourceConfig`. `AUTO` defaults to `true` for multi-node steps (`num_nodes > 1`) and `false` for single-node steps. Users can override explicitly if needed.

`dnsPolicy` is derived automatically and never exposed to users:
- `hostNetwork: true` → `dnsPolicy: ClusterFirstWithHostNet` (required or cluster DNS breaks with host networking)
- `hostNetwork: false` → `dnsPolicy: ClusterFirst`

**Files changed:**
- `src/seekr_chain/config.py` — added `host_network` field to `ResourceConfig`
- `src/seekr_chain/backends/argo/jobset.py` — resolve `AUTO` to bool in `_build_role_context`, pass into role context dict
- `src/seekr_chain/backends/argo/templates/jobset.yaml.j2` — template both `hostNetwork` and `dnsPolicy` from context
- `tests/unit/test_manifest_rendering.py` — three new tests covering AUTO single-node, AUTO multi-node, and explicit override

**Gotcha:** `trim_blocks=True` in the Jinja2 env eats the newline after `{% endif %}` when used inline (e.g. `dnsPolicy: {% if x %}A{% else %}B{% endif %}`), merging the next line onto it and producing invalid YAML. The fix is to use the `{{ 'A' if x else 'B' }}` expression form instead, which uses `{{ }}` tags that are not affected by `trim_blocks`.
