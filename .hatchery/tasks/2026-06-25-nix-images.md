# Task: nix-images

**Status**: complete
**Branch**: hatchery/nix-images
**Created**: 2026-06-25 15:15

## Objective

seekr-chain originally required users to build/push a Docker image and
reference it from the job config. The pull side of that pattern has two
inherent ceilings: registry pull speed (capped even from local mirrors)
and sequential layer extraction inside the pod. As ML training images
grow (multi-GB ROCm+pytorch+FA bases plus thin top layers for
per-experiment deps), those ceilings dominate startup time.

This branch ships **nix-mode roles** as an alternative: a role declares
a nix expression (typically a flake), seekr-chain evaluates it locally
to compute a content-addressed `/nix/store/<hash>-<name>` closure path,
and the pod boots from a tiny "nix-runner" OCI image that fetches the
closure from a binary cache at startup. Wins:

- Per-path parallel fetches from object storage (vs. sequential layer pulls).
- Content-addressed cross-image deduplication of store paths.
- Push only the changed store paths (e.g. bumping `transformers` uploads
  ~megabytes, not multi-GB) — automatic, no Dockerfile layer ordering needed.
- Warm-node hostPath caching: once a closure is on a node, consumer pods
  scheduled to the same node skip the fetch entirely.

Validated end-to-end at scale by example 10 (two-node ROCm all-reduce
bandwidth test), which hits ~297 GB/s — matching the image-mode baseline
on the same fabric, with the closure fetching in seconds instead of the
multi-minute docker-image pull.

## Context

- nix on the submit machine evaluates the user's expression and produces
  a deterministic closure hash. If the closure isn't already in the binary
  cache, seekr-chain injects a synthetic build step at the front of the
  DAG; the user's nix-mode step `depends_on` it. The build step runs on
  the cluster, builds the closure, pushes it to the cache, and exits.
- The runtime is the `seekr-chain-nix-runner` OCI image (built from
  `docker/Dockerfile.nix-runner`). It ships nix + s5cmd + nothing else;
  the user's actual deps land at runtime from the closure.
- nix-in-unprivileged-container needs **both** `sandbox = false` and
  `filter-syscalls = false` in `/etc/nix/nix.conf`. Either alone trips
  the container runtime's default seccomp profile (which blocks
  `seccomp(2)`, which both flags use). The runtime image bakes this in;
  the chain-nix-init script writes the same config at runtime.
- Cache: native nix `s3://` protocol against a configured bucket
  (`nix_store` in `~/.seekrchain.toml` or per-step `nix.store`). nix
  rejects path prefixes on `s3://` URIs — bucket must be bare
  (`s3://my-bucket?region=...`). seekr-chain validates this at submit.

## Summary

### Architecture

- **`NixConfig` schema** (`src/seekr_chain/config.py`): adds a `nix:`
  field to `RoleSpecConfig`. Mutually exclusive with `image:` — a role
  is either image-mode or nix-mode, never both. Required field
  `expression: str` (default `"./"`) is interpreted as a path relative
  to `code.path`; the same string is used for submit-time eval and
  inside the build pod's `nix build` invocation. Optional `store`,
  `build`, `system`, `attr`, `build_resources`.
- **Submit-time pre-pass** (`src/seekr_chain/nix_resolution.py`):
  `resolve_nix_steps()` walks every nix-mode role, validates that
  `nix.expression` is contained in `code.path`, evaluates the closure
  hash via `nix eval`, checks the configured store via an S3 HEAD on
  `<store>/<hash>.narinfo`, and synthesizes one build step per unique
  missing closure (deduped across roles that share an expression).
  `depends_on` wires each consumer to its build step.
- **`chain-nix-init` init container** (rendered in
  `templates/_nix_init_container.yaml.j2`, script in
  `resources/chain-nix-init.sh`): runs after `chain-init` (which
  downloads the resource bundle), mounts the shared `/nix` volume at
  `/nix-shared`, runs `nix copy --from $store $closure` with a
  size-watching watchdog (kills the pull if no progress for 2 minutes,
  or 30 minutes total) and three attempts. Prints a summary distinguishing
  "already on node" (warm cache) from "pulled fresh."
- **Main container** runs the user's script under the nix-runner image's
  `/bin/sh`, with `PATH=$CLOSURE/bin:$PATH` and
  `LD_LIBRARY_PATH=$CLOSURE/lib:$LD_LIBRARY_PATH` exported. The
  closure's RPATH-baked references to `/nix/store/<hash>/lib` resolve
  via the mounted volume; `LD_LIBRARY_PATH` is a fallback for `dlopen()`
  calls that resolve unqualified library names (RCCL → libibverbs, etc.).
- **Warm-node caching via closure-hash podAffinity**: every pod that
  consumes or produces a given closure carries the label
  `seekr-chain.nix/closure: <hash>` and a soft podAffinity
  (`preferredDuringSchedulingIgnoredDuringExecution`, weight 50,
  topology=`kubernetes.io/hostname`) targeting other pods with the same
  label. The scheduler prefers nodes where the closure has already been
  fetched — turning the per-node hostPath store into a free warm cache.
- **hostPath store volume**: shared at `/var/lib/seekr-chain/nix` by
  default. Consumer pods mount at `/nix` with `subPath=nix` so
  `chain-nix-init`'s chroot writes (which land at
  `/nix-shared/nix/store/<hash>` on disk) surface at `/nix/store/<hash>`
  in main — exactly where the closure's RPATHs expect them. Build pods
  mount the same volume at `/nix-shared` (no subPath) and use
  `--store local?root=/nix-shared` to direct writes into the same
  on-disk location. emptyDir is supported as a fallback for clusters
  whose PodSecurity doesn't admit hostPath.
- **GHCR-published nix-runner image**: published via
  `.github/workflows/build-nix-runner-image.yml` against
  `docker/nix-runner.version`. Pinned in
  `nix_resolution._DEFAULT_NIX_RUNNER_IMAGE` with sha256 digest.

### Patterns established

- **Script source lives in `resources/`**: `chain-nix-init.sh` and
  `nix-build.sh` ship as standalone files copied into every job's
  upload bundle (mirroring `fluentbit.sh`). Per-job parameters get
  passed via env vars (`SEEKR_CHAIN_NIX_STORE`, `SEEKR_CHAIN_NIX_CLOSURE`,
  `SEEKR_CHAIN_NIX_EXPRESSION`, `_SYSTEM`, `_ATTR`, `_COMPRESSION`) on
  the container, not baked into the script as f-string substitutions.
  This keeps the rendered manifest readable and the scripts independently
  editable.
- **`nix.expression` is one path string, interpreted relative to
  `code.path`** on both sides of the wire. Submit-side eval and pod-side
  `nix build` get the same string. Lexical containment check
  (`os.path.normpath`) rejects `../escape` paths but allows symlinks
  inside `code.path` to escape (the upload follows symlinks and brings
  the content along).
- **Build step is image-mode with env-var markers, not a nix-mode role
  itself.** The whole point of the build step is to *create* the
  closure, so closure-fetch semantics don't apply. `_detect_closure_hash`
  sees the build step's env, attaches the same closure-hash label, and
  the same podAffinity preference applies — so consumer pods naturally
  cluster on the node that ran the build.
- **Size-bounded GC at end of pull.** `chain-nix-init` runs
  `resources/nix-gc.sh` after a successful pull. If `du -sk /nix-shared`
  exceeds `SEEKR_CHAIN_NIX_STORE_MAX_BYTES` (default 128 GiB, configurable
  via `user_config.nix_store_max_size`), it delegates to `nix store gc
  --max <overage>`. The just-pulled closure is symlinked at
  `/nix-shared/nix/var/nix/gcroots/seekr-chain/active` so nix's GC
  protects it + all its transitive deps; everything else is fair game.
  Best-effort: GC failures (lock contention, etc.) don't fail the pod.

### Files added / changed

- `src/seekr_chain/config.py` — `NixConfig`, `RoleSpecConfig` image/nix mutex.
- `src/seekr_chain/nix_resolution.py` — submit-time pre-pass.
- `src/seekr_chain/nix_utils.py` — `eval_closure_path`, `closure_exists`,
  `closure_hash_from_path`.
- `src/seekr_chain/user_config.py` — `nix_store`, `nix_runner_image`,
  `nix_store_volume_kind`, `nix_store_hostpath`, `nix_compression` fields.
- `src/seekr_chain/backends/argo/jobset.py` — `_resolve_nix_role`,
  `_select_role_runtime`, `_detect_closure_hash`, closure-hash affinity.
- `src/seekr_chain/backends/argo/templates/jobset.yaml.j2` — closure-hash
  label, podAffinity, nix-store volume; init container in a separate
  partial (`_nix_init_container.yaml.j2`) via `{% include %}`.
- `src/seekr_chain/backends/argo/resources/chain-nix-init.sh`,
  `nix-build.sh` — runtime scripts.
- `docker/Dockerfile.nix-runner`, `docker/nix-runner.version` — runtime image.
- `.github/workflows/build-nix-runner-image.yml` — GHCR publish workflow.
- `examples/6_nix_runtime` … `examples/10_nix_bandwidth_test` — five
  examples covering single-node, multi-node, ROCm, torchrun, and the
  bandwidth test that validates the fast path.
- `tests/unit/test_nix_*.py`, `tests/integration/core/test_nix_job.py`,
  `tests/test_code/7_nix_basic/` — schema + rendering + injection +
  end-to-end coverage.

### Gotchas a future agent should know

- **`/nix/store` is absolute, content-addressed, and arch-specific.**
  A closure built for `x86_64-linux` cannot run on `aarch64-linux`. The
  `nix.system` field defaults to `x86_64-linux`; set it explicitly on
  ARM clusters.
- **The nix-runner image's `/nix` is shadowed by the hostPath mount.**
  This is fine for consumer pods because main doesn't need the image's
  nix tooling (it runs the user's binaries from the closure). The build
  pod NEEDS the image's nix tooling, so it mounts the hostPath at
  `/nix-shared` (not `/nix`) and uses `--store local?root=/nix-shared`
  to direct writes into the chroot while keeping image's `/nix` intact.
- **GC is per-pod opportunistic, not periodic.** Cleanup runs at the end
  of `chain-nix-init` only when a pod pulls a closure that pushes the
  node over budget. A node with no recent pulls keeps its existing
  closures indefinitely. Set `nix_store_max_size` tight if disk pressure
  is a concern; otherwise the natural pod churn handles it.
- **nix's s3 store rejects path prefixes.** Use `s3://bucket?region=...`,
  not `s3://bucket/some/prefix`. seekr-chain validates this at submit
  with a clear error. If you need a prefix, give the cache its own bucket.
- **AWS_REQUEST_TIMEOUT matters.** The chain-nix-init script sets a
  10-minute per-request timeout on the AWS SDK; without it, a stalled
  TCP connection looks like "slow but progressing" forever. Combined
  with the size-growth watchdog, this catches real hangs in <2 minutes.
- **Closure-baked env vars use bash's `:=` operator** so runtime overrides
  still win. See `examples/10_nix_bandwidth_test/flake.nix`'s
  `tuned-torchrun` wrapper for the pattern — `:` `${NCCL_IB_GID_INDEX:=3}`
  sets the default only if the var is unset.

### Followups (not blocking)

1. **HTTP binary cache daemon backed by Seekr-fs / OCI.** Replaces the
   bare-s3 cache with an HTTP service that nix can talk to natively, on
   top of any object storage backend. URL swap; no other code change.
2. **Build-pod mount layout.** Considered: install nix at a non-`/nix`
   path in the runner image (e.g. `/nix-shared`), mount hostPath at
   `/nix` proper, copy bootstrap → `/nix` once per node, build directly
   into the real `/nix` store (no `--store local?root=` chroot, no path-
   validation issue). **Deferred** — the alternatives all have larger
   trade-offs than the cost they save:
   - Building nix with `--with-store-dir=/nix-shared` means losing
     cache.nixos.org for every transitive dep of nix's tooling layer
     (multi-hour fresh builds at image-build time).
   - OverlayFS (image `/nix` as lower, hostPath as upper, merged at
     mount) sidesteps the path-validation issue cleanly but requires
     cluster-level OverlayFS support that isn't universal.
   - The actual cost of the current pattern is bounded by closure size,
     not "all of /nix" — only the user's closure lands in hostPath, and
     only on cache-miss builds. Warm-node hits skip the copy entirely.

3. **Tighter runtime isolation: per-pod `/nix` containing only the
   declared closure.** Current architecture mounts the node's full
   hostPath `/nix` into every pod — so a pod can see (and theoretically
   execute) other closures present on the same node alongside its
   declared one. Two architectural approaches were spiked in-cluster on
   2026-06-29 against example 10's 14 GB closure on warm nodes:

   **A. Hardlink into per-pod emptyDir** (`cp -al` from hostPath
      `/var/lib/seekr-chain/nix` to an emptyDir `/nix-isolated`).
      Conceptually free: hardlinks share inodes, no data copy, no extra
      disk usage. **Blocked on this cluster.** Result: `cp -al` raises
      `EXDEV` (Invalid cross-device link) on every file — hostPath and
      emptyDir land on different filesystems on these nodes. 0 paths
      hardlinked, 87696 failures. Would require per-pod hostPath subPaths
      (under `/var/lib/seekr-chain/nix-pods/<pod-uid>/`) to share a
      filesystem with the source, which widens per-node hostPath surface
      area and needs explicit cleanup on pod exit.

   **B. Full `cp -r` into per-pod emptyDir.** Result: 14.38 GB / 221
      paths copied in **13 s** at 1.15 GB/s sustained on NVMe nodes.
      Faster than the rough 60-180s "small-file I/O bound" estimate, but
      still re-adds ~12 s to chain-nix-init's warm-cache path (which is
      currently ~1-2 s after the fast-path optimization). Also doubles
      per-pod disk: 14 GB hostPath + 14 GB emptyDir per running pod.

   **C. Anthropic-style bind-mount sidecar.** Long-running sidecar
      (k8s 1.29+ native sidecar via `restartPolicy: Always` on an init
      container) bind-mounts only the active closure's paths into a
      shared volume that main consumes. Requires `privileged: true` on
      the sidecar (k8s enforces this for `mountPropagation: Bidirectional`).
      Not spiked in-cluster.

   **Decision (v1): defer all three.** Internal-trust cluster; no
   observed isolation violation; no incoming requirement. Approach A is
   architecturally cleanest but the filesystem layout doesn't support
   it. Approach B is unprivileged but expensive (re-adds the ~12 s
   warm-cache cost + 2× per-pod disk). Approach C is the most flexible
   but adds a privileged container to every nix-mode pod (security
   surface + admission policy work).

   **Revisit triggers**: multi-tenant cluster, security review demand,
   observed closure-cross-contamination incident, or a workload pattern
   that benefits from /nix being a clean view of just the declared
   closure (debugging, reproducibility audit).
