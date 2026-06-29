# Multi-node all-reduce bandwidth test (nix runtime)

Two-node, 16-GPU all-reduce that reports algorithm + bus bandwidth
across several buffer sizes. Verifies the RDMA path is active and the
cluster's IB/RoCE fabric delivers its rated throughput (~300 GB/s bus
on MI300X + Mellanox ConnectX-7).

## What unlocks the fabric

Four things, all surfaced via the example's `config.yaml`:

1. **`pkgs.rdma-core`** in the closure. RCCL `dlopen`s `libibverbs.so`
   at startup to talk to the IB stack; rdma-core provides it.
2. **`security.privileged: true`**. Grants `/dev/infiniband` access
   (`ibv_open_device`). Without this RCCL can't initialize verbs.
3. **`host_network: true`**. Lets the pod see the host's IB interfaces.
   The default CNI's pod network namespace doesn't expose them.
4. **Three RCCL env vars**: `NCCL_IB_GID_INDEX=3` (RoCE v2 GID — the
   single biggest knob on Mellanox CX-6/CX-7), `NCCL_CROSS_NIC=1`,
   `NCCL_NCHANNELS_PER_PEER=8`. These spread channels across the
   cluster's 9 RoCE NICs instead of pinning each rank-pair to one NIC.

Without (1)-(3), RCCL falls back to TCP and caps at single-digit GB/s.
Without (4), you get IB transport but stay at ~37 GB/s single-NIC.
With all four, ~280-300 GB/s on this cluster.

## Run

```sh
cd examples/10_nix_bandwidth_test
chain submit config.yaml --follow
```

If you've run examples 8 or 9 against the same cluster, most of the
closure is shared and the build step skips. The only new path here is
`rdma-core` (~10 MB).

## Expected output

```
size         algo BW          bus BW
  8.00MB     31.79 GB/s      59.60 GB/s
 64.00MB     90.69 GB/s     170.04 GB/s
512.00MB    152.11 GB/s     285.21 GB/s
  2.00GB    158.33 GB/s     296.87 GB/s
```

Bus bandwidth approaches the fabric's rated speed at larger buffers
(smaller buffers are latency-bound and won't saturate). Compare to the
image-mode reference test
(`tests/integration/distributed/test_bandwidth.py::test_multi_node`)
which gets ~280 GB/s on the same hardware via the `rocm/pytorch` image.

## Troubleshooting

When something's wrong, the fastest diagnosis is to add
`NCCL_DEBUG: "INFO"` to the step's `env:` block and re-submit. RCCL
then prints its transport-selection decisions to stderr, which surface
in the pod logs. The minimal config keeps DEBUG off to keep logs clean.

Common failure modes with their NCCL_DEBUG signatures:

| Symptom in NCCL_DEBUG output | Meaning | Fix |
|---|---|---|
| `NET/Socket : Using [0]eth0 ...` | RCCL fell back to TCP (no IB) | Confirm `privileged: true`, `host_network: true`, and that `rdma-core` is in the closure. |
| `Failed to open libibverbs.so[.1]` | RCCL can't find libibverbs | Verify `pkgs.rdma-core` is in `flake.nix` paths and seekr-chain is auto-exporting `LD_LIBRARY_PATH=<closure>/lib`. |
| `Channel ... via NET/IB/<N>/GDRDMA` for all channels with same N | Single-NIC ceiling | The three RCCL env vars aren't taking effect. Verify they're under the step's `env:` block, not `nix.env`. |
| `Using network IB` but bus BW stays ~37 GB/s | NCCL_IB_GID_INDEX is wrong | RoCE v2 is GID index 3 on most Mellanox cards; verify with `show_gids` on a node. |

For a deeper dive into how this config was tuned (which env vars
mattered, which detours didn't), see the **Tuning log** section
below.

## Tuning log: how this config got to ~280 GB/s

This example didn't reach the working image-mode baseline on the first
try. The path from "0 GB/s, plugin doesn't load" to "~297 GB/s matches
image mode" took six discrete failure modes, each diagnosed against
NCCL_DEBUG output and fixed. Recorded here so future readers don't
re-derive it.

### 1. `/bin/sh: stat: no such file or directory` (runc init failure)

**Symptom.** Pod container creation failed at runc init time, before any
script ran:

```
runc create failed: ... exec: "/bin/sh": stat /bin/sh: no such file or directory
```

**Why.** The nix-runner image's `/bin/sh` was a symlink chain
`/bin/sh → /root/.nix-profile/bin/sh → /nix/var/nix/profiles/default/bin/sh
→ /nix/store/<bash>/bin/sh`. When the hostPath volume gets mounted at
`/nix` in the main container, that target chain dangles if the volume
doesn't contain the exact same store paths the image was built against.

**Fix.** Bake static busybox into the nix-runner image at `/bin/sh`,
`/bin/bash`, etc. — real binaries that don't traverse `/nix` at all.
The runner image becomes self-sufficient for the shell/POSIX-tools
surface area regardless of what the closure volume contains. (Lives in
`docker/Dockerfile.nix-runner`.)

### 2. RCCL falls back to TCP — `Failed to open libibverbs.so`

**Symptom.** Multi-node bandwidth caps at ~5 GB/s. NCCL_DEBUG shows:

```
NCCL INFO Failed to open libibverbs.so[.1]
NCCL INFO NET/Socket : Using [0]eth0 ... [1]rdma0 ...
```

**Why.** RCCL `dlopen()`s `libibverbs.so` at init time to talk to the
RDMA stack. The closure ships rdma-core (which provides libibverbs),
but nixpkgs's RCCL has no RUNPATH pointing at rdma-core's lib dir, so
the bare-name dlopen falls through to the system search path and finds
nothing.

**Fix.** Auto-export `LD_LIBRARY_PATH=<closure>/lib` for nix-mode main
containers in `seekr-chain` itself (parallels the existing TLS cert
env vars). `buildEnv` merges all input pkgs' `/lib` into the closure
root's `lib/`, so this one path covers libibverbs, libamdhip64, and
everything else nix-packaged libs might dlopen.

### 3. `NET/Plugin: Could not find: libnccl-net.so`

**Symptom.** Plugin loading log line:

```
NCCL INFO NET/Plugin: Could not find: libnccl-net.so.
NCCL INFO RCCL PXN set as disabled
```

PXN (multi-NIC fan-out) gets disabled because the AMD-recommended net
plugin isn't found. Single-NIC bandwidth ceiling.

**Why.** The plugin (libnccl-net.so) is a separate AMD package from
RCCL itself. It's not in nixpkgs at all. AMD's docker image bundles it;
the nix closure didn't.

**Fix.** Build aws-ofi-nccl (`github.com/aws/aws-ofi-nccl`) from upstream
in a `mkDerivation` inside `flake.nix`. For ROCm builds, upstream
installs `librccl-net-ofi.so`. RCCL dlopens by the name `libnccl-net.so`
(NCCL convention, kept for back-compat). `postInstall` adds symlinks
to bridge:

```nix
postInstall = ''
  ln -sf librccl-net-ofi.so $out/lib/libnccl-net.so
  ln -sf librccl-net-ofi.so $out/lib/librccl-net.so
'';
```

### 4. `m4/get_version.sh failed` and `bash: applet not found`

Two adjacent failures during the `aws-ofi-nccl` build:

**a.** `m4/get_version.sh failed (1) — No version found`. The upstream's
autotools build looks for `.release_version`, `git describe`, or
`BRAZIL_PACKAGE_CHANGE_ID`. `fetchFromGitHub`'s archive has none of
them.

**Fix.** Write `.release_version` in `postPatch` (must be postPatch, not
preConfigure — autoreconfHook runs aclocal between them, which is what
calls get_version.sh):

```nix
postPatch = ''
  echo "${version}" > .release_version
'';
```

**b.** `bash: applet not found` during autoreconf. The build sandbox's
`#!/bin/bash` shebangs were going to busybox, which doesn't have a
`bash` applet — it has `ash`. The `/bin/bash` symlink → busybox was a
broken stub.

**Fix.** In the nix-runner image, symlink `/bin/bash` to the real bash
from the nix profile (`/root/.nix-profile/bin/bash`) instead of to
busybox. Other tools (sh, head, awk, sed, grep, ps, ...) still point at
busybox.

### 5. aws-ofi-nccl loaded but: `No eligible providers were found, Selected provider is tcp`

**Symptom.** Plugin loads cleanly. But:

```
NCCL WARN NET/OFI No eligible providers were found
NCCL WARN NET/OFI Selected provider is tcp, fabric is 10.224.0.0/12 (found 8 nics)
```

aws-ofi-nccl found libfabric, asked it for RDMA-capable providers,
got nothing, fell back to libfabric's `tcp` provider. Bandwidth ~37
GB/s — actually *worse* than the previous fall-back-to-native-NET/IB
case because tcp-over-libfabric has more overhead than RCCL's own
NET/IB path.

**Why.** nixpkgs's `libfabric` package builds without `--enable-verbs`
and doesn't include `rdma-core` in `buildInputs`. configure's
auto-detection finds no libibverbs at build time, silently disables
verbs, ships a libfabric with only tcp/sockets providers.

**Fix.** Override `pkgs.libfabric` to inject rdma-core + an explicit
`--enable-verbs` flag, and link aws-ofi-nccl against the override:

```nix
libfabricWithVerbs = pkgs.libfabric.overrideAttrs (old: {
  buildInputs = (old.buildInputs or []) ++ [ pkgs.rdma-core ];
  configureFlags = (old.configureFlags or []) ++ [ "--enable-verbs" ];
});
```

### 6. `NET/OFI Couldn't register memory region with regattr. RC: -38, Function not implemented`

**Symptom.** Plugin now reports `Selected provider is verbs;ofi_rxm,
fabric is IB-..., found 20 nics`. But during the actual all-reduce
setup, one rank crashes:

```
NET/OFI Couldn't register memory region with regattr. RC: -38, ERROR: Function not implemented
NET/OFI Unable to register memory (type = 2) for device 19
```

`-38` is `ENOSYS`. The plugin called `fi_mr_regattr` (libfabric's
extended memory-registration API), and the underlying verbs driver
on the cluster's kernel doesn't implement it.

**Why.** aws-ofi-nccl 1.20 defaults to its newer `SENDRECV` transport
protocol, which uses extended MR features (likely DMA-BUF + raw mode)
that this cluster's verbs driver doesn't support. Older `RDMA`
transport protocol uses simpler MR semantics.

**Fix.** `OFI_NCCL_PROTOCOL: "RDMA"` in `config.yaml`'s `env:`. Once
set, aws-ofi-nccl tries the RDMA protocol; that path also has issues
on this fabric (the plugin reports "Initialized NET plugin IB" but
RCCL ultimately uses its native NET/IB anyway), but at least no
crash. Result: clean run at ~37 GB/s single-NIC. Better than crashed.

### 7. PXN disabled, all 4 channels on same NIC, ~37 GB/s

**Symptom.** Clean run, no errors. NCCL_DEBUG shows:

```
NCCL INFO RCCL PXN set as disabled
NCCL INFO Connected all rings, use ring PXN 0 GDR 1
NCCL INFO Channel 00/0 : 7 -> 8 [send] via NET/IB/8/GDRDMA
NCCL INFO Channel 01/0 : 7 -> 8 [send] via NET/IB/8/GDRDMA   ← same NIC
NCCL INFO Channel 02/0 : 7 -> 8 [send] via NET/IB/8/GDRDMA   ← same NIC
NCCL INFO Channel 03/0 : 7 -> 8 [send] via NET/IB/8/GDRDMA   ← same NIC
```

All 4 channels per rank pair share ONE NIC. The cluster has 9 active
mlx5 NICs, only one is being used. Bandwidth caps at single-NIC speed
(~37 GB/s on this hardware).

**Why (and what actually fixed it).** RCCL's defaults don't try to
spread channels across multiple NICs unless explicitly told to. The
image-mode RCCL build (in `rocm/pytorch:rocm7.2_*`) must have
different defaults or topology heuristics that engage automatically.
nixpkgs's RCCL doesn't.

**Fix.** Three env vars together unlocked it:

```yaml
NCCL_CROSS_NIC: "1"               # allow rings to traverse different NICs
NCCL_NCHANNELS_PER_PEER: "8"      # more channels = more NIC distribution opportunity
NCCL_IB_GID_INDEX: "3"            # RoCE v2 GID, not RoCE v1 (default 0)
```

The decisive one is **`NCCL_IB_GID_INDEX=3`**. RCCL's default of 0
picks RoCE v1, which on Mellanox CX-6/CX-7 hardware has dramatically
worse performance than RoCE v2. Setting it to 3 (the standard RoCE v2
GID index on these cards) alone gets most of the way to the working
baseline; the cross-NIC and channels-per-peer flags are belt-and-
suspenders multi-NIC encouragers.

After these three: **~297 GB/s at 2 GB buffer**. Matches the image-
mode `test_bandwidth.py::test_multi_node` reference (~280 GB/s).

### Final state

The minimal set of changes from a fresh nix-mode workflow to working
multi-node RDMA at fabric speed:

1. Closure includes `pkgs.rdma-core` for libibverbs.
2. `security.privileged: true` + `host_network: true` for IB access.
3. seekr-chain auto-exports `LD_LIBRARY_PATH=<closure>/lib` (already wired in).
4. RCCL env: `NCCL_IB_GID_INDEX=3` (the single most impactful var on
   RoCE clusters).
5. RCCL env: `NCCL_CROSS_NIC=1` + `NCCL_NCHANNELS_PER_PEER=8` for
   redundant multi-NIC encouragement.

The `aws-ofi-nccl` plugin, `libfabricWithVerbs` override, and
`OFI_NCCL_PROTOCOL=RDMA` env are all still in the example — but they
aren't doing meaningful work in the final config. RCCL's native NET/IB
is what serves the traffic. The plugin path was an architectural
dead-end on this combination of (nixpkgs RCCL + nixpkgs libfabric +
cluster verbs driver); the productive work was in tuning RCCL itself.

A future cleanup could simplify the flake by removing the plugin
derivation and the libfabric override. For now they're harmless and
documented.

### Lessons applicable to other nix-mode + RDMA setups

- **Always set `NCCL_IB_GID_INDEX=3` on RoCE fabrics.** RCCL's default
  is wrong for modern Mellanox cards. This is the single fix that
  moves the needle most.
- **Trust RCCL's native NET/IB before reaching for aws-ofi-nccl.**
  The plugin is necessary on AWS EFA (where there's no IB device) and
  helps on tightly-coupled HPC clusters with verbs-extension support,
  but for vanilla RoCE-over-Mellanox it's a maintenance burden with
  no measurable gain.
- **Match `NCCL_DEBUG=INFO` lines to the actual data path being used.**
  "Loaded plugin Libfabric" and "Using network IB" mean different
  things and indicate different code paths. Search for "Initialized NET
  plugin <X>" to see which one actually handled traffic.
