# Task: emudhar

**Status**: complete
**Branch**: emudhar
**Created**: 2026-10-28 10:17

## Objective

Mitigate the eMudhra/emSign certificate incident across application dependencies,
managed runtime images, development tooling, and rendered Nix workloads.

## Summary

### Decisions and patterns

- Follow [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2):
  reject the compromised emSign ECC Root CA - G3 trust anchor, not all emSign
  roots. Certifi 2026.10.19 restored unaffected roots; the pinned 2026.10.23
  bundle includes G1. Public G3/G1 fixtures test rejection/compatibility and
  contain no private keys.
- Require application certifi >=2026.10.19; update uv.lock to 2026.10.23. Managed
  images bootstrap hash-verified certifi 2026.10.23 and pip 26.2.2 on pinned
  Python 3.12.16. CI selects uv from tool.uv.required-version >=0.13.4;
  container uv binaries remain pinned by version and image digest.
- Standardize managed system trust on the verified certifi bundle at
  /etc/ssl/certs/ca-certificates.crt. Remove old hashed directory entries too,
  since replacing only the concatenated file can leave alternate trusted roots.
  Python gates inspect system/application/pip-vendored roots and tool versions;
  a POSIX-shell hash gate also runs in Python-free final images. Stable two-/
  three-component versions are supported; prereleases remain rejected.
- Preserve Nix store hashes, shared-volume semantics, and the runner account ABI.
  Select the checked bundle outside /nix in init/builder images and in rendered
  consumer SSL_CERT_FILE, REQUESTS_CA_BUNDLE, and NIX_SSL_CERT_FILE defaults.
  Explicit workflow/step CA overrides win without duplicate names; step wins
  over workflow. Computed store/closure identity remains authoritative.
- Refresh approved image trust with `uv run inv refresh-certificates`: install
  only hash-verified bootstrap wheels temporarily, enforce security floors/G3
  rejection, compute the bundle hash, and align controller/dev requirements.
  Never derive the image hash from application certifi. Application updates are
  independent; bootstrap changes require deliberate hash review and image gates.
- Supply paired .in/.txt requirements for bootstrap/controller/dev dependencies.
  The root uv Dependabot updater discovers direct-child docker/ requirements;
  dev/docker/ is explicitly included. The developer toolbox copies uv/uvx from
  the official digest-pinned image and hash-locks OCI CLI plus all dependencies.
- Keep resolver-generated lock metadata: exceptiongroup is selected only below
  Python 3.11, so normalization of its typing-extensions edge does not broaden
  installs through that dependency. Project Python >=3.10 support is unchanged.
- Keep .hatchery/Dockerfile.pi and this exact task path intentionally. The
  sandbox is separate from application runtime images. REVIEW_RESPONSE.md
  contains a proposed reply to every review comment.

### Published and pinned images

All names are under ghcr.io/seekr-technologies/. These are OCI **index** digests,
not individual platform manifests or attestations.

| Image | Version | Index digest |
| --- | --- | --- |
| seekr-chain-controller | 1.1.1 | sha256:bd0217f98bdd035fcf6806a3888bf46fa79386fe531becd809e61cb8111b6dae |
| seekr-chain-init | 1.0.1 | sha256:2cc7f2d50ad40296cb5cb66aaf13fa00aad8fdab2f1edf92baeafcc6f6a9efe3 |
| seekr-chain-nix-runner | 0.3.2 | sha256:45ecc5e79ff34b8a196639bf2677ac20d6494c04c513086c63a2090340b0e946 |

The three successful publication workflows built commit
230760a97b5ccd338efc8ef18885e6339ce748f5:
[controller](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081769441),
[init](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081820122),
[nix-runner](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081831839).
Independently verified GitHub success/source revision, GHCR index-content hashes,
and linux/amd64 + linux/arm64 availability for all three supplied references.
Defaults in launch_k8s_workflow.py, jobset.py, and nix_resolution.py now pin them.

### Files and validation

- Dependency metadata/locks; docker/ Dockerfiles, version files, requirements,
  build_gates, final-container tests, and README; dev/docker/ toolbox;
  scripts/refresh_certificates.py and tasks.py; CI/docs/release/Dependabot config;
  Nix JobSet env construction; SECURITY.md and REVIEW_RESPONSE.md.
- Tests cover actual rendered Nix specs and override precedence, vulnerable/
  unaffected roots, version parsing, independent shell-gate behavior, refresh
  failures, hash pinning, and tooling/update configuration. Retain workflow gate
  checks; actual image builds replace redundant Dockerfile-text assertions.
- Final validation after digest replacement: **1007 unit tests passed**;
  formatting/lint, uv lock --check, and git diff --check passed. Refresh ran
  successfully twice; isolated OCI CLI installation reported 3.74.0. Docker was
  unavailable locally; successful image workflows provide CI build/certificate
  checks and native runner smoke-test evidence, not a local Kubernetes rollout.

### Operational caveats

Release/install the updated package, refresh configured image overrides, and
recreate affected workloads. Existing pods keep their old images. Custom
closures, private/embedded trust stores, and explicit CA overrides remain the
operator's responsibility; refresh host certificates and existing pip/uv
installations separately. Do not mutate immutable Nix store paths or disable
TLS verification. Future image-content changes need new unused tags and newly
verified index digests; publication workflows refuse tag replacement.
