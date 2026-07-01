# Torchrun on ROCm (nix runtime, 2 nodes)

Two-node distributed all-reduce — same closure shape as
`examples/8_nix_torchrun`, just with `num_nodes: 2` in `config.yaml`.
seekr-chain handles the rest:

- Each pod gets `NNODES`, `NODE_RANK`, `MASTER_ADDR`, `MASTER_PORT` env
  vars; rank-0 pod's hostname is the rendezvous address.
- Both pods land in the same JobSet and share a DNS subdomain so
  `MASTER_ADDR` resolves.

## Run

```sh
cd examples/9_nix_torchrun_multinode
chain submit config.yaml --follow
```

Expected output (interleaved across both pods):

```
[step--0-0] === launching torchrun ===
[step--0-1] === launching torchrun ===
[0/16] Before all-reduce: 1.0
[0/16] After all-reduce: 136.0
```

(All-reduce sum over ranks 1..16 = 136.)

## Warm-node behavior

If both pods land on a node where the same closure has been used
before (e.g. the build step from a previous run lived there, or
example 8 ran on it), `chain-nix-init` will report `copying 0 paths`
and main starts within a few seconds. Cold pods pull the full closure
from your `nix_store` over the network — ROCm closures are large
(~5–8 GB), so first-run startup is in the minutes range.

## What's different from example 8

Only `config.yaml`:

```diff
 resources:
+  num_nodes: 2
   gpus_per_node: 8
```

The flake and `job.py` are byte-identical. That's the point: scaling a
nix-runtime workload to more nodes is a one-line config change, same
as image-mode workloads. The closure is content-addressed, so both
nodes share the cache hit when their `chain-nix-init` pulls the same
hash from `nix_store`.
