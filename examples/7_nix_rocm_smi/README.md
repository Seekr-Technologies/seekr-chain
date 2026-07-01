# ROCm smoke test (nix runtime)

Verify a nix-mode pod can talk to AMD GPUs on the cluster — closure contains
just `rocm-smi`, no Python, no PyTorch. If this prints a GPU inventory, the
foundation is sound for moving to torch + training closures.

## Run

```sh
cd examples/7_nix_rocm_smi
chain submit config.yaml --follow
```

Expected output:

```
=== rocm-smi --version ===
ROCM-SMI version: 1.X.Y
ROCM-SMI-LIB version: 6.X.Y

=== rocm-smi (GPU inventory) ===
========================= ROCm System Management Interface =========================
================================== Concise Info ====================================
GPU  Temp   AvgPwr  ...
...
```

If `rocm-smi` prints "ERROR: GPU not found" or hangs, the pod isn't seeing
the AMD kernel driver / `/dev/kfd` / `/dev/dri/*`. Check `gpu_type` is set
to `amd.com/gpu` in `config.yaml` and that the cluster's device plugin is
exposing those paths to pods (existing image-mode workflows in
`examples/2_torchrun` are the reference — if they work, this should).

## What's in the closure

Just three things, plus their transitive deps:

- `rocmPackages.rocm-smi` — the SMI tool.
- `bash` + `coreutils` — so the user-mode `#!/bin/sh script` can run.
- `cacert` — placeholder, not actually used here but kept consistent with
  other examples; remove if you don't want it.

`rocm-smi` doesn't need the rest of the ROCm runtime (no HIP, no MIOpen) —
it only talks to the kernel driver. That keeps this closure small.
