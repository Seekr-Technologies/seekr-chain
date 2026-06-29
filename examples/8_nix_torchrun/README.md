# Torchrun on ROCm (nix runtime, single node)

Same workload as `examples/2_torchrun` — distributed all-reduce across the
GPUs of one node — but the runtime is a nix closure instead of the
`rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.7.1` Docker
image. PyTorch in the closure is the ROCm variant from nixpkgs.

## Run

```sh
cd examples/8_nix_torchrun
chain submit config.yaml --follow
```

First submit will build the closure in-cluster — this can take a while
(15–30 min on a cold node) because PyTorch's ROCm build pulls a large
substituter footprint (gcc, stdenv, ROCm runtime libs). Subsequent runs
reuse the cached closure: pod startup is just the `nix copy --from` of
the runtime paths.

Expected output:

```
=== GPU inventory ===
[rocm-smi output for the assigned GPU(s)]
=== launching torchrun ===
[0/8] Before all-reduce: 1.0
[0/8] After all-reduce: 36.0
```

(All-reduce sum for ranks 1..8 = 1+2+3+4+5+6+7+8 = 36.)

## ROCm + nixpkgs caveats

nixpkgs's pytorch-rocm story is functional but somewhat finicky:

- **`config.rocmSupport = true` is required.** It steers `pkgs.python3.torch`
  to the ROCm variant. Without it you'd get a CUDA build that fails to
  load HIP libraries on AMD hardware.
- **Pin nixpkgs to a branch that has the ROCm version you want.** As of
  mid-2026 `nixos-26.05` ships rocm-smi 7.2.3 and the matching
  pytorch-rocm. Older release branches lag (24.05 had ROCm 6.0). If
  26.05's ROCm release predates a fix you need, jump to
  `nixos-unstable` and accept the moving target — `flake.lock`
  pins the exact revision either way.
- **Match the cluster's driver ABI.** The kernel driver on the node
  (visible via `cat /sys/module/amdgpu/version` if amdgpu is loaded)
  needs to be compatible with the ROCm userspace in the closure.
  Generally ROCm N userspace works against driver N or N-1; bigger
  gaps are roulette.
- **First build is slow.** Even with `cache.nixos.org` as a substituter,
  the ROCm closure pulls a lot of paths the first time. seekr-chain's
  closure-hash podAffinity helps subsequent pods land on the same warm
  node so `chain-nix-init`'s `nix copy --from` is a no-op.

If pytorch-rocm via nixpkgs proves too brittle for your environment,
the fallback pattern is to use a docker-image step (`examples/2_torchrun`)
for the GPU-heavy workload and reserve the nix-closure pattern for
CPU/lighter steps where the architectural wins (incremental push,
parallel fetch) matter more than the build-time pain.
