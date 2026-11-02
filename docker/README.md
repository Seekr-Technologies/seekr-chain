# Certificate security and image rollout

The eMudhra/emSign incident is tracked by
[GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2).
The application requires certifi >=2026.10.19. Images bootstrap pip 26.2.2
and certifi 2026.10.23 using wheel hashes, even if the base installer has stale
roots. CI selects uv from `tool.uv.required-version` in `pyproject.toml`
(minimum patched release: 0.13.4). Container binaries remain pinned by digest.

## Trust-store pattern

All three release Dockerfiles use the same `certificates` stage. It exports the
hash-verified certifi bundle as `/etc/ssl/certs/ca-certificates.crt`. Old hashed
certificate-directory entries are removed too: replacing only the concatenated
bundle could leave compromised roots available through OpenSSL's CA directory.
This intentionally standardizes the managed images on Mozilla/certifi roots;
private organizational roots are not added by these builds.

- `build_gates/gate_certificates.py` checks certifi/pip/uv minimum stable versions,
  parses system/application/pip-vendored bundles, and rejects the compromised
  emSign ECC Root CA - G3 certificate by its DER SHA256 fingerprint. Unaffected
  emSign roots remain allowed, matching upstream's restoration in 2026.10.19.
- `build_gates/gate_system_bundle.sh` verifies the exact approved bundle hash,
  including in Python-free BusyBox images. Refresh its hash with
  `uv run inv refresh-certificates` after reviewing bootstrap pin/hash updates.
- Both bootstrap and final build stages run gates. `test-certificates.sh` also
  executes them in final containers in PR CI and before image publication.
- Alpine download stages use the checked bundle before downloading s5cmd.
- Nix init/builder images use `NIX_SSL_CERT_FILE`, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
  `CURL_CA_BUNDLE`, and `ssl-cert-file` in nix.conf to select roots outside `/nix`.
  Rendered Nix consumer JobSets also default `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
  and `NIX_SSL_CERT_FILE` to `/etc/ssl/certs/ca-certificates.crt`, never closure
  `nss-cacert`. Explicit workflow/step env may override these defaults (step wins);
  operators then own the safety of that alternate bundle. Clients with private
  embedded bundles still need their own updates. Do not rewrite
  immutable CA files in Nix store paths: that invalidates their content hashes.
  The existing Nix toolchain/account ABI remains unchanged. Custom Nix closures
  and programs that ignore these settings need separate rebuilds/updates.

The test fixtures `emsign-ecc-root-g3.pem` (rejected) and `emsign-root-g1.pem`
(accepted) under `tests/unit/fixtures/` are public CA certificates, not private
keys. They cover both compromised-root rejection and unaffected-root compatibility.

## Build and verify

From the repository root, on a Docker/BuildKit host:

```sh
for kind in controller init nix-runner; do
  docker build --no-cache --pull -f "docker/Dockerfile.$kind" -t "seekr-chain-$kind:certificates" docker
  sh docker/test-certificates.sh "seekr-chain-$kind:certificates" "$kind"
done
sh docker/test-nix-runner.sh seekr-chain-nix-runner:certificates
# Dev image requires repository-root context.
docker build --no-cache --pull -f dev/docker/dockerfile -t seekr-chain-dev:certificates .
```

Controller dependencies are generated with:

```sh
uv pip compile docker/controller-requirements.in --generate-hashes --python-version 3.12 -o docker/controller-requirements.txt
```

The standalone developer toolbox (`dev/docker/dockerfile`) is not a CI/runtime
image. It copies uv/uvx from the official image pinned by version and manifest
digest. OCI CLI and its transitive dependencies are hash-locked separately:

```sh
uv pip compile dev/docker/requirements.in --generate-hashes --python-version 3.12 -o dev/docker/requirements.txt
```

### Certificate pin maintenance

Dependabot's root uv updater discovers the direct-child `docker/` requirements;
`dev/docker/` is explicitly configured as an additional directory. The bootstrap
now has a matching `.in` source so uv can regenerate its hashed `.txt` output.
For deliberate bootstrap upgrades:

```sh
uv pip compile docker/certificate-requirements.in --generate-hashes --python-version 3.12 --upgrade -o docker/certificate-requirements.txt
# Review versions and artifact hashes first; then synchronize managed images.
uv run inv refresh-certificates
uv run inv format --check
uv run pytest tests/unit
```

The refresh command installs only hash-verified bootstrap wheels into a temporary
directory, checks security floors and rejects G3, computes the actual bundle hash,
and aligns/regenerates controller/dev requirements without unrelated upgrades.
It never derives the approved image hash from application `certifi`/`uv.lock`.
Application certifi can therefore update independently. Review generated diffs
and run the uncached image builds above before accepting a new bootstrap bundle.
A Dependabot bootstrap update intentionally needs this reviewed hash-refresh
step; CI will not silently approve a changed trust bundle.

## Published security baseline

The three image workflows succeeded from commit `230760a97b5ccd338efc8ef18885e6339ce748f5`.
The application defaults now pin these published multi-platform OCI index digests;
GHCR contents were independently checked for both `linux/amd64` and `linux/arm64`.
All images are under `ghcr.io/seekr-technologies/`.

| Image | Tag | Index digest |
| --- | --- | --- |
| seekr-chain-controller | 1.1.1 | `sha256:bd0217f98bdd035fcf6806a3888bf46fa79386fe531becd809e61cb8111b6dae` |
| seekr-chain-init | 1.0.1 | `sha256:2cc7f2d50ad40296cb5cb66aaf13fa00aad8fdab2f1edf92baeafcc6f6a9efe3` |
| seekr-chain-nix-runner | 0.3.2 | `sha256:45ecc5e79ff34b8a196639bf2677ac20d6494c04c513086c63a2090340b0e946` |

Successful publication workflows (including certificate checks):
[controller](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081769441),
[init](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081820122),
[nix-runner](https://github.com/Seekr-Technologies/seekr-chain/actions/runs/41081831839).

## Future releases and operator rollout

Updating source defaults does **not** replace running pods or configured overrides.
For subsequent image changes:

1. Bump the appropriate `docker/*.version` to a new unused tag, then run the
   manual image build workflows from the reviewed branch. The workflows refuse
   existing tags and rebuild without layer caching. Confirm native smoke tests
   and both platform builds succeed before using their outputs.
2. Inspect each published multi-platform manifest and record its digest, e.g.
   `docker buildx imagetools inspect ghcr.io/seekr-technologies/seekr-chain-init:1.0.1`.
3. Update `_DEFAULT_CONTROLLER_IMAGE` in
   `src/seekr_chain/backends/k8s/launch_k8s_workflow.py`, `_DEFAULT_INIT_IMAGE` in
   `src/seekr_chain/backends/k8s/jobset.py`, and `_DEFAULT_NIX_RUNNER_IMAGE` in
   `src/seekr_chain/nix_resolution.py` to the new **tag and manifest digest**.
   Do not reuse old digests or invent unpublished ones. Run unit/integration
   tests and release seekr-chain only after this step.
4. Update configured image overrides as well (`SEEKRCHAIN_INIT_IMAGE`,
   `SEEKRCHAIN_CONTROLLER_IMAGE`, `SEEKRCHAIN_NIX_RUNNER_IMAGE`, workflow-level
   controller images, and custom role images). Recreate affected pods/workflows;
   existing pods retain old image contents. Rebuild custom images/closures and
   refresh host system trust stores and existing virtual environments.
5. Upgrade pip >=26.2.2 and uv >=0.13.4 separately in existing environments.
   Updating application certifi does not update installers' private trust stores.
   Follow the [Python security guidance](https://discuss.python.org/t/information-for-python-users-regarding-emudhra-emsign-ca-certificate-security-incident/109576)
   for hash-verified bootstrapping. Do not disable TLS verification. Python
   3.12.16 or the corresponding supported patched interpreter adds defense in depth;
   this change does not alter seekr-chain's Python >=3.10 metadata support policy.

The security baseline above is published and pinned in source. Operators must
still release/install the updated package, refresh image overrides and custom
closures, and recreate affected workloads to replace previously running images.
