# Nix-mode Runtime

Nix-mode lets you replace your step's Docker image with a **nix closure** —
the content-addressed output of evaluating a nix expression. The pod boots
from a tiny runtime image and pulls only what your job actually needs from
a nix binary cache, in parallel.

## Why nix-mode?

Docker images have two bottlenecks for large ML environments:

1. **Sequential layer extraction.** Even with parallel layer downloads, the
   container runtime extracts layers sequentially. A 10 GB image stack is
   minutes of startup latency.
2. **All-or-nothing layers.** Bumping `transformers` rebuilds a 100 MB
   layer; nodes that already have 99% of your dependencies still pull the
   whole thing.

Nix-mode flips this:

- **Per-path parallel fetches** from object storage. A 14 GB / 220-path
  closure pulls in under a minute on a cold node, ~1 s on a warm one.
- **Content-addressed deduplication.** Two jobs sharing `torch` share the
  same `/nix/store/<hash>-torch` path — fetched once per node, used by
  every pod.
- **Push only what changed.** Bumping `transformers` re-uploads ~megabytes
  to the cache, not multi-GB.
- **Warm-node caching for free.** Closures land on the node's local
  hostPath; subsequent pods on the same node skip the fetch entirely.

## Quick start

A nix-mode step looks like this:

```yaml
name: my-job
namespace: argo-workflows

code:
  path: ./    # uploaded with the job; flake.nix lives here

steps:
  - name: train
    nix:
      expression: ./    # flake at the root of code.path
    script: python train.py
```

Alongside `config.yaml`, you need a `flake.nix` describing your runtime:

```nix
{
  description = "My training environment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.05";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);
    in {
      packages = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.buildEnv {
            name = "my-train-env";
            paths = [
              (pkgs.python312.withPackages (ps: with ps; [ torch transformers ]))
              pkgs.bash
              pkgs.coreutils
              pkgs.cacert    # required for HTTPS clients in the closure
            ];
          };
        });
    };
}
```

Submit:

```bash
chain submit -f config.yaml --follow
```

If the closure isn't in your configured binary cache, seekr-chain
automatically injects an in-cluster build step that runs `nix build`
and pushes the result. Subsequent submits hit the cache and skip the
build.

!!! note "Local nix required"
    Submit needs `nix` on PATH to evaluate the expression's closure path
    locally. Install from <https://nixos.org/download>. Eval is pure
    (no compilation, no system-specific code), so a Mac can resolve an
    `x86_64-linux` closure path — only the realization runs in-cluster.

## How it works

There are four moving parts at submit time, three at pod runtime.

### Submit-time pipeline

1. **Evaluate the closure.** seekr-chain runs `nix eval` locally on your
   `nix.expression` to get the content-addressed `/nix/store/<hash>-<name>`
   path. This is what determines whether the closure is "in cache" — the
   hash changes whenever any input changes.

2. **Validate `nix.expression` is inside `code.path`.** The same string is
   used at submit time and inside the build pod (which runs `nix build`
   from `/seekr-chain/workspace`). Lexical containment check rejects
   `../escape` paths; symlinks inside `code.path` are allowed because the
   code upload follows them.

3. **Check the binary cache.** A single S3 HEAD on
   `<store>/<closure-hash>.narinfo` tells us whether to inject a build
   step.

4. **Inject a build step if missing.** If the closure isn't in the cache
   and `nix.build` is `true` (default), seekr-chain prepends a synthetic
   step to the workflow that builds + pushes. The user's step gets a
   `depends_on` pointing at the build, so Argo schedules them in order.

5. **Query warm nodes.** seekr-chain lists pods in the cluster carrying
   the label `seekr-chain.nix/closure=<hash>` (set on every nix-mode pod
   we've ever rendered) and collects their node names. These get
   injected as a soft `nodeAffinity` preference on the new pod, steering
   the scheduler toward nodes that already have the closure on disk.

### Pod-time pipeline

When the pod starts:

1. **`chain-init`** (init container) downloads the job's asset bundle from
   s3. Same as image-mode.

2. **`chain-nix-init`** (init container, runs after chain-init):
    - If the closure root + all transitive deps are already at
      `/nix-shared/nix/store/<hash>` (warm cache from a previous pod on
      this node), it exits in ~1 s without touching s3.
    - Otherwise it runs `nix copy --from <store> <closure>`, with a
      watchdog that kills hung pulls (stall >120 s or wall >30 min) and
      retries up to 3 times.
    - On completion, marks the closure as a nix gc-root and runs the
      size-bounded GC over the hostPath store.

3. **`main`** (the user's container) mounts the same volume at `/nix`
   (with `subPath=nix` so paths surface as `/nix/store/<hash>` — what
   the closure's RPATHs were baked against). The entrypoint exports
   `PATH=$CLOSURE/bin:$PATH` and `LD_LIBRARY_PATH=$CLOSURE/lib:...`,
   then runs your script.

### The runtime image

Nix-mode pods use a minimal "nix-runner" OCI image
(`docker/Dockerfile.nix-runner`) that ships:

- `nix` 2.21.1 (from `nixos/nix`) — does its own s3 fetches via the
  baked-in aws-sdk-cpp, so no separate s3 client is needed
- `bash` from nix's default profile
- A static busybox at `/bin/` for the standard POSIX tools

Image config:

- `experimental-features = nix-command flakes` baked into `/etc/nix/nix.conf`
- `sandbox = false` baked in (k8s container can't nest sandboxes)
- `filter-syscalls = false` set at runtime by `chain-nix-init.sh`
  (must match `sandbox = false`; both are needed for nix to operate in
  an unprivileged k8s container without tripping the runtime's default
  seccomp profile, which blocks `seccomp(2)`)

Nothing else. Everything your job actually needs comes from the closure.

### The closure-fetch pipeline visualized

```
┌─────────────────────────────────────────────┐
│ submit machine                              │
│   nix eval ./#packages.x86_64-linux.default │
│     → /nix/store/abc1234-my-env             │
│   s3 HEAD <store>/abc1234.narinfo           │
│     → 404 (missing)                         │
│   inject build step                         │
└─────────────────────────────────────────────┘
                  ↓
┌─────────────────────────────────────────────┐
│ build pod (image-mode, in-cluster)          │
│   cd /seekr-chain/workspace                 │
│   nix build path:./#...                     │
│   nix copy --to local?root=/nix-shared      │  ← warms hostPath
│   nix copy --to s3://store?compression=zstd │  ← durable cache
└─────────────────────────────────────────────┘
                  ↓ (depends_on)
┌─────────────────────────────────────────────┐
│ user pod                                    │
│   chain-init: download assets               │
│   chain-nix-init:                           │
│     [fast path] path-info --recursive       │
│       all present → skip s3                 │
│     [normal]   nix copy --from s3           │
│     nix-gc.sh (size-bounded, async-safe)    │
│   main: exec user script with closure       │
│         on PATH + LD_LIBRARY_PATH           │
└─────────────────────────────────────────────┘
```

## Configuration reference

Every field on `nix:` is documented in the auto-generated [Configuration
Reference → NixConfig](../reference/configuration.md#nixconfig). Global
defaults (the `nix_store`, `nix_runner_image`, etc. you'd set in
`~/.seekrchain.toml` or via `SEEKRCHAIN_*` env vars) are documented on
the same page under **UserConfig**.

A typical nix-mode step:

```yaml
steps:
  - name: train
    nix:
      expression: ./
      # attr: default            # default
      # system: x86_64-linux     # default
      # store: s3://my-cache     # optional; falls back to user config
      # build: true              # default — auto-inject build step if missing
      # build_resources:         # optional — bump for heavy native builds
      #   cpus_per_node: 32
      #   mem_per_node: 256G
    script: python train.py
```

A typical `~/.seekrchain.toml`:

```toml
nix_store = "s3://my-cache"
# nix_store_max_size = "128GiB"   # default
# nix_compression    = "ZSTD"     # default
```

## Architecture details

### Mount layout

The hostPath store on every node lives at `/var/lib/seekr-chain/nix/`.
Pods see two different views of it depending on their role:

| Role | Mount point | subPath | Why |
|---|---|---|---|
| Consumer (your nix-mode role) | `/nix` | `nix` | `chain-nix-init` writes via `--store local?root=/nix-shared`, which lands paths on disk at `/nix-shared/nix/store/<hash>`. With `subPath=nix`, that path surfaces in main at `/nix/store/<hash>` — exactly where the closure's binaries' RPATHs expect. |
| Builder (auto-injected) | `/nix-shared` | (none) | The build script uses `--store local?root=/nix-shared` directly, matching the consumer's on-disk layout. |
| chain-nix-init init container | `/nix-shared` | (none) | Needs to call `nix copy --from` writing to the chroot location. |

### Closure detection on the pod

The pod's main container needs to know what closure it's running. seekr-chain
sets two env vars:

- `SEEKR_CHAIN_NIX_CLOSURE`: `/nix/store/<hash>-<name>` (resolved at submit)
- `SEEKR_CHAIN_NIX_STORE`: the binary cache URI

The entrypoint exports `PATH=$SEEKR_CHAIN_NIX_CLOSURE/bin:$PATH` and
`LD_LIBRARY_PATH=$SEEKR_CHAIN_NIX_CLOSURE/lib:...` before running your script.
Most binaries don't need `LD_LIBRARY_PATH` (nix bakes RPATH); it's a fallback
for `dlopen` calls that resolve unqualified library names (e.g. RCCL loading
`libibverbs.so.1`).

### Warm-cache scheduling

When you submit a job, seekr-chain queries the k8s API for **pods that
carry the closure label** (`seekr-chain.nix/closure=<hash>`), regardless of
phase. Completed pods stick around for the workflow TTL (7 days by default)
with their `spec.nodeName` populated — so they serve as a record of "this
node had this closure on disk recently."

The list of node names goes into the new pod as a soft `nodeAffinity`
preference (weight 90):

```yaml
nodeAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
  - weight: 90
    preference:
      matchExpressions:
      - key: kubernetes.io/hostname
        operator: In
        values: [node-1, node-3, node-7, ...]
```

The scheduler scores those nodes higher; if any can fit the pod, it lands
there and the chain-nix-init fast path triggers (~1 s startup). If they're
all saturated, the pod lands on a cold node and pulls fresh — degraded but
not blocked.

There's also a per-closure `podAffinity` (weight 50) for **concurrent**
co-scheduling: multiple pods in the same submit that share a closure attract
toward each other's node.

### Build-step dedup

If multiple roles in one workflow share a closure (e.g. a multi-role step
where all roles use the same env), seekr-chain synthesizes **one** build
step shared by all consumers via `depends_on`. The build step name is
deterministic (`nix-build-<hash[:12]>`) so two submits that need the same
closure produce identical step names — no duplication.

### GC

`chain-nix-init` runs `resources/nix-gc.sh` at the end of every pull. It:

1. Reads on-disk size of `/nix-shared` (or uses the value chain-nix-init
   just computed for its summary, to avoid a redundant `du`).
2. If under budget (`nix_store_max_size`, default 128 GiB), exits.
3. Otherwise: writes a symlink to the just-pulled closure under
   `/nix-shared/nix/var/nix/gcroots/seekr-chain/active` (so nix's GC
   considers it live), then calls `nix store gc --max <overage>`.

nix's GC respects gcroots: the active closure and all its transitive deps
are protected; everything else (older pulls' closures) is eligible to
delete. Failures (lock contention, etc.) don't fail the pod.

## Recipes

### Lock your inputs

Commit your `flake.lock` alongside `flake.nix`. The lock pins exact
nixpkgs commits; without it, every submit gets a fresh nixpkgs which
re-evaluates to a different closure hash and re-triggers the build.

```bash
nix flake lock
git add flake.nix flake.lock
```

### Bake env vars into the closure

The closure-fetch wrapper sets `PATH` and `LD_LIBRARY_PATH`, but anything
your binary checks at runtime (NCCL settings, RCCL tuning, OMP threads)
isn't there by default. Use `pkgs.writeShellScriptBin` to wrap your
entrypoint with the env you want — the wrapper goes into the closure and
runs first.

```nix
let
  tuned-torchrun = pkgs.writeShellScriptBin "torchrun" ''
    # ':=' sets the default only if unset, so user can still override at submit
    : ''${NCCL_IB_GID_INDEX:=3}
    : ''${NCCL_CROSS_NIC:=1}
    export NCCL_IB_GID_INDEX NCCL_CROSS_NIC
    exec ${python}/bin/torchrun "$@"
  '';
in
pkgs.buildEnv {
  name = "my-env";
  paths = [
    (pkgs.lib.hiPrio tuned-torchrun)   # win the torchrun symlink collision
    python
    pkgs.bash pkgs.coreutils pkgs.cacert
  ];
}
```

`lib.hiPrio` makes the wrapper's `bin/torchrun` win over the python
package's `bin/torchrun` in the buildEnv symlink tree.

### Beefy build resources

The auto-injected build step uses modest defaults (4 CPU, 16 GiB RAM, no
GPU). Native builds (FA, custom kernels, pytorch from source) need much
more:

```yaml
steps:
  - name: train
    nix:
      expression: ./
      build_resources:
        num_nodes: 1
        cpus_per_node: 32
        mem_per_node: 256G
        ephemeral_storage_per_node: 500G
        gpus_per_node: 0
    script: python train.py
```

### Pre-build manually, refuse auto-build

For workflows where you want the build to happen in CI or by hand and
fail-fast otherwise:

```yaml
steps:
  - name: train
    nix:
      expression: ./
      build: false   # don't inject a build step; error at submit if missing
    script: python train.py
```

### TLS in the closure

If your job makes HTTPS requests (pip, requests, urllib, curl), the
closure needs `pkgs.cacert`. The runtime image's `/nix` is shadowed by
the closure mount, so the image's CA bundle isn't reachable. seekr-chain
sets `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, and `NIX_SSL_CERT_FILE` to
`$CLOSURE/etc/ssl/certs/ca-bundle.crt` — but that path only exists if
`cacert` is in your closure.

```nix
pkgs.buildEnv {
  name = "...";
  paths = [
    # ...your deps...
    pkgs.cacert    # ← required for HTTPS
  ];
}
```

## When to use nix-mode

**Use nix-mode when:**

- Your image is large (>2 GB) and pod startup dominates short-job latency
- You iterate on dependencies often and Docker layer rebuilds are slow
- Multiple jobs share most of their dependencies and you want
  cross-image deduplication
- You want exact reproducibility (closure hash uniquely identifies the
  runtime)

**Stick with image-mode when:**

- Your runtime is a small base image with a few pip installs (closure
  pull overhead isn't worth the bookkeeping)
- You don't have `nix` on your submit machine and don't want to install
  it
- You need an OS-level capability that doesn't fit in a nix closure
  (e.g. systemd, custom kernel modules, FUSE filesystems)
- Your team isn't comfortable maintaining `flake.nix` for their
  environments

## Trade-offs vs image-mode

| Axis | Image-mode | Nix-mode |
|---|---|---|
| Cold pod startup | sequential layer extract (mins on multi-GB images) | parallel per-path fetch + warm-cache (~10-60s) |
| Warm pod startup | full image layer cache | ~1-2 s (path-info check only) |
| First-time setup | none | install nix locally; write `flake.nix` |
| Dependency bump | rebuild full image layer (or careful layering) | upload only changed store paths (~MB) |
| Image bloat | accumulates unused layers | only what's actually depended on |
| Build-time | docker build, your hardware | in-cluster, on whatever build_resources you set |
| Visibility | `docker history` | `nix path-info --closure-size` |
| Reproducibility | image digest | closure hash (deterministic from flake + lock) |
| Storage on registry | per-image full layers | per-path content-addressed (deduplicated) |
| Cluster setup | nothing | binary cache (e.g. s3 bucket) + RBAC for chain to access it |

## Troubleshooting

### `nix.expression must be a path relative to code.path`

The path you passed to `nix.expression` is absolute. Use a relative path
under `code.path` — both submit-side eval and the build pod's `nix build`
interpret it the same way (relative to `code.path` / `/seekr-chain/workspace`).

### `closure /nix/store/<hash> is not in store ..., and nix.build=False`

Closure isn't in the binary cache and you've disabled auto-build. Either:

- Pre-build manually (e.g. `nix copy --to s3://store $(nix build --print-out-paths ./)`), or
- Set `nix.build: true` to let seekr-chain inject a build step.

### `nix's s3:// store does not support path prefixes`

You set `nix_store` to something like `s3://bucket/some/prefix`. nix's
native s3 store doesn't honor path prefixes — give the cache its own
bucket. Use `s3://bucket?region=us-east-1` (bare bucket + query params).

### Build step succeeds but consumer can't find `libfoo.so`

The closure exports `lib/` from each input, but `LD_LIBRARY_PATH` only
covers `dlopen` of unqualified names. If your binary has a hardcoded
`/usr/lib/libfoo.so` path, nix isn't involved — fix the build or use
`pkgs.runCommand` to patchelf the binary.

### "Closure already fully present on node" but main container still slow to start

That's the chain-nix-init fast path working correctly. Look at the main
container's image pull (not the nix-runner image — your `main_image`,
if you set one) and at any post-start hooks.

### Warm-cache nodeAffinity not steering pods

Run:

```bash
# At submit time, check which pods currently carry your closure label
kubectl get pods -A -l seekr-chain.nix/closure=<your-hash> -o wide
```

If the list is empty, no pods have been on the cluster with that closure
recently (workflow TTL is 7 days by default — older pods are gone). The
first submit pays the cold pull; subsequent ones get the affinity.

If the list has pods but new pods still land on cold nodes, check:

- Are the listed nodes resource-constrained? Affinity is a *preference*,
  not a constraint — saturated nodes get filtered out at Filter time
  before scoring.
- Does your job have its own affinity rules? Pack affinity (`affinity:
  [{type: POD, direction: ATTRACT, group: ...}]`) sums with the closure
  affinity; they can conflict if the pack group is on cold nodes.

## Limitations

- **No isolation between pods sharing a node.** Every pod's `/nix` mount
  is the node's full hostPath store. A pod can read (and theoretically
  execute) other closures present on the same node. Acceptable in an
  internal-trust cluster; not yet supported for multi-tenant. See the
  branch ADR `2026-06-25-nix-images.md` for the design discussion.
- **Closure must be a flake.** Plain `.nix` files work for eval but the
  build step's `path:<expression>#packages.<system>.<attr>` ref requires
  a flake.
- **Submit machine needs `nix`.** Eval is local. No way around this
  without a remote eval service. Install from
  <https://nixos.org/download>.
- **`nix` is x86_64/aarch64 Linux only on the cluster side.** macOS and
  Windows clusters aren't supported (the runtime image is Linux).
- **Closure path is system-specific.** Build for the system that matches
  your cluster (`nix.system`); a closure built for `x86_64-linux` won't
  run on `aarch64-linux`.
- **No periodic GC.** The hostPath store's GC runs only when a new pod
  pulls a closure that pushes the node over budget. A node with no
  recent pulls keeps its existing closures indefinitely.

## Examples

The repo's `examples/` directory has several nix-mode workflows:

- `examples/6_nix_runtime/` — minimal python env
- `examples/7_nix_rocm_smi/` — ROCm tooling smoke test
- `examples/8_nix_torchrun/` — single-node torchrun
- `examples/9_nix_torchrun_multinode/` — multi-node torchrun
- `examples/10_nix_bandwidth_test/` — multi-node ROCm all-reduce
  bandwidth test (the perf demonstrator: ~297 GB/s on
  MI300X + ConnectX RoCE, matching image-mode baseline)
