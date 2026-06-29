# Task: nix-images

**Status**: complete (POC stage 1)
**Branch**: hatchery/nix-images
**Created**: 2026-06-25 15:15

## Objective

seekr-chain currently requires users to build/push a docker image and
reference it from the job config. The pull side of that pattern has two
inherent ceilings: registry pull speed (capped even from our own mirrors)
and sequential layer extraction inside the pod. As ML training images
grow (multi-GB ROCm+pytorch+FA bases plus thin top layers for
per-experiment deps), those ceilings become the bottleneck.

Alternative architecture (per a public Anthropic lecture): define the
runtime environment as a **nix expression**, build it ahead of time, push
the resulting **nix closure** to object storage, and have the k8s pod use
a tiny bootstrap image that fetches the closure at startup. Wins:

- Per-path parallel fetches from object storage (vs. sequential layer pulls).
- Content-addressed cross-image deduplication of store paths.
- Push only changes the new store paths (e.g. bumping `transformers`
  uploads ~megabytes, not multi-GB), automatically — no Dockerfile layer
  ordering required.

The POC's purpose was to validate the loop end-to-end (build → push →
pod fetch → exec) with seekr-chain's actual cluster, without yet
templatizing the pattern into seekr-chain itself.

## Context

- Build host: Apple Silicon Mac. ThreatLocker blocked the Determinate
  installer's Keychain access, so nix was installed via the upstream
  installer with flakes enabled manually. Cross-compiling
  `aarch64-darwin → x86_64-linux` is fragile (python especially), so the
  actual builds run inside a `nixos/nix:2.21.1` podman container on the
  Mac — flake mounted in, persistent docker volume at `/nix` to warm the
  store across runs, AWS creds forwarded via env + `~/.aws` mount.
- Two nix-in-container gotchas surfaced during setup, both seccomp:
    1. `sandbox = false` alone isn't enough; nix also has an independent
       `filter-syscalls` flag, on by default, that installs its own BPF
       program. Podman's default seccomp profile blocks `seccomp(2)`,
       which trips both. The fix is **both** `sandbox = false` **and**
       `filter-syscalls = false` in nix.conf inside the container.
    2. Setting these via `NIX_CONFIG=$'…\n…'` doesn't reliably work —
       podman truncates newline-containing env values. Writing
       `/etc/nix/nix.conf` from inside the container's startup script is
       the unambiguous fix. Same applies to the `Dockerfile.nix-runner`
       image (baked at image-build time).
- Cache: native nix `s3://` against `seekr-ml-taw` (us-east-1) — a
  shared bucket. nix's S3 store puts objects (`<hash>.narinfo`,
  `nar/*.nar.xz`, `nix-cache-info`) at the bucket root with
  content-addressed names, so coexisting with other content is fine in
  practice (no collisions). A dedicated bucket would be cleaner for
  lifecycle/metrics separation; deferred.
- Briefly explored a `file://` + `s5cmd sync` indirection to put the
  cache under a key prefix inside an existing bucket. Reverted in
  commit `2d8b3db` because the production target is the HTTP cache
  daemon (Seekr-fs over OCI), under which nix talks the binary cache
  HTTP protocol natively. Keeping the POC on pure `nix copy --to s3://`
  + `nix copy --from s3://` means the production migration is a single
  URL swap (`s3://...` → `http://cache.internal`) with no flow changes.

## Verified result

End-to-end run on 2026-06-25:

- Closure: `seekr-chain-nix-poc-env` (python 3.12.7 + requests 2.31.0 +
  coreutils + bash). 39 store paths.
- Pushed via `build-in-docker.sh` to `s3://seekr-ml-taw?region=us-east-1`.
- Runner image (`k8-nexus.cb.ntent.com:7443/ntent/seekr-chain/nix-runner:poc`)
  built + pushed via `run.sh --skip-build --skip-push`.
- Pod in `argo-workflows` namespace; uses the existing `aws-creds` secret.
- `nix copy --from` pulled all 39 paths in **9.976s wall**; pod exec'd
  python from the closure and printed the expected versions.

For this small closure that timing is similar to a docker pull of an
equivalent image — the architectural win shows up at scale, not on a
toy. The point of the run was to prove the **loop works**, not to
demonstrate a speedup.

## Summary

### Key decisions

- **Pure native nix protocol on push and pull.** No `file://` or `s5cmd`
  intermediaries in the steady-state POC, because the production target
  (HTTP cache daemon backed by Seekr-fs over OCI) is also pure-nix-
  protocol from nix's point of view. Migration is a URL swap.
- **Single container in the pod, not init/main split.** `/nix/store`
  paths are absolute, so the volume holding the closure must be mounted
  at `/nix/store` in any container that exec's from it — but doing that
  shadows the runner image's built-in nix tooling. POC sidesteps by
  doing fetch + exec in one container with the image's `/nix/store`.
  Production seekr-chain integration needs a different shape (see
  README "Production gaps → 1").
- **Build host = podman container on the Mac.** Cross-compile would
  have meant fighting python builds; remote-builder setup would have
  meant fighting ThreatLocker. Podman + `nixos/nix` image is reliable
  and survives across iterations via a persistent named volume at `/nix`.

### Patterns established

- `nix.conf` for nix-in-unprivileged-container needs **both**
  `sandbox = false` and `filter-syscalls = false`. One isn't enough.
- Configure nix in-container by writing `/etc/nix/nix.conf` from the
  startup script. Don't try `NIX_CONFIG` with multi-line values through
  podman — newlines get eaten.
- `nix copy --to s3://bucket?region=…` and `nix copy --from s3://…`
  work with the standard AWS SDK credential chain (env vars, `~/.aws`,
  IMDS). Mounting `~/.aws:/root/.aws:ro` into the build container
  covers long-lived keys and the SSO cache simultaneously.
- The runner image (`Dockerfile.nix-runner`) deliberately ships with
  nothing but `nix` and the nix.conf tweaks. Everything else lands at
  runtime from the binary cache.

### Files added (under `nix_poc/`)

- `flake.nix` + `flake.lock` — closure definition.
- `Dockerfile.nix-runner` — bootstrap image.
- `manifest.yaml` — k8s Job template with `__PLACEHOLDERS__`.
- `build-in-docker.sh` — Mac/podman build+push path.
- `run.sh` — native-nix-host orchestrator; also handles the
  runner-image build/push and manifest render (with `--skip-build
  --skip-push`) when the build happened elsewhere.
- `.gitignore` — covers `/result`, `.closure`, rendered manifest.
- `README.md` — walkthrough + production gaps + next steps.

### Gotchas a future agent should know

- **Closure paths are absolute and arch-specific.** A closure built for
  `x86_64-linux` cannot run on `aarch64-linux` or darwin and vice
  versa. The flake outputs both linux arches; ensure you pick the right
  one for your cluster.
- **The `buildEnv` closure root is tiny (~580K)** because it's a
  symlink tree pointing into the transitive deps. Don't mistake that
  for the actual fetched data — the transitive paths together are
  hundreds of MB.
- **The runner image's `/nix/store` shadows any volume mounted at
  `/nix/store`.** Don't mount an emptyDir at `/nix/store` and expect
  nix to keep working. The init/main split that seekr-chain's pattern
  wants needs a different volume strategy — see README Production gap #1.
- **The pod doesn't get cross-pod store reuse.** Every pod's
  `/nix/store` is the runner image's own. Warm-node dedup needs either
  `hostPath`, a node-affinity PVC, or `nix-snapshotter`. Out of scope
  for this POC, but the most important production decision for actual
  speedup.
- **AWS creds: use existing `aws-creds` secret** in `argo-workflows`.
  Same one seekr-chain workloads use. The manifest hardcodes that name.

### Next steps (in priority order)

1. **Seekr-fs HTTP cache daemon.** The architectural unlock. Resolves
   OCI transport, prefix limitations, and the substituter-as-build-cache
   gap simultaneously. Nix points at one URL; the daemon translates
   nix binary cache HTTP ↔ OCI. Reference implementation to fork or
   study: `harmonia`.
2. **Real closure**: repackage one ROCm+pytorch+FA training image as a
   nix expression. The numbers from a 5 GB closure are what will
   actually inform whether to commit to this architecture.
3. **Templatize into seekr-chain**: add `nix:` field to `RoleSpecConfig`,
   teach the JobSet renderer to emit the closure-fetch pattern with the
   init/main split (mounted `/nix` volume, runner image with relocatable
   nix tooling). This is the work that turns the POC into a real
   seekr-chain feature.

Items 1 and 2 are independent and can proceed in parallel. Item 3
depends on at least 1 being designed (since the manifest shape changes
between `s3://` and the daemon's HTTP URL is trivial, but the init/main
split design is non-trivial and informs the templated form).
