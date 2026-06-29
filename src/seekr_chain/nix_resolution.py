"""Submit-time resolution of nix-mode steps in a WorkflowConfig.

Called from ``launch_argo_workflow`` after config validation, before manifest
rendering. Responsibilities:

1. For every role with ``nix.expression``, evaluate locally to compute the
   ``/nix/store/<hash>-<name>`` closure path and cache it back into
   ``role.nix.closure``. Eval requires ``nix`` on PATH; if it isn't, the
   error from :mod:`seekr_chain.nix_utils` is surfaced verbatim.

2. For every nix-mode role, check whether its closure is already in the
   configured binary cache. Closures that aren't there *and* have
   ``nix.build = True`` get a build step synthesized.

3. Synthesize one build step per *unique* missing closure (dedup across
   roles that happen to share the same closure). Wire the user steps'
   ``depends_on`` to point at the synthesized build step. The build steps
   are inserted at the *start* of the workflow's step list; ordering
   doesn't actually matter (depends_on drives DAG execution) but it keeps
   the rendered workflow easy to read.

Mutates the passed ``WorkflowConfig`` in place and returns it.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Optional

from seekr_chain import nix_utils
from seekr_chain.config import (
    MultiRoleStepConfig,
    NixConfig,
    ResourceConfig,
    RoleSpecConfig,
    SingleRoleStepConfig,
    WorkflowConfig,
)
from seekr_chain.user_config import config as _user_config

logger = logging.getLogger(__name__)

# Modest defaults — fits a small python closure on a typical worker node.
# Large native builds (pytorch from source, FA, ROCm packages) should set
# `nix.build_resources` explicitly with more CPU / RAM.
_DEFAULT_BUILD_RESOURCES = ResourceConfig(
    num_nodes=1,
    cpus_per_node=4,
    mem_per_node="16G",
    gpus_per_node=0,
)


def _roles_of(step) -> list[RoleSpecConfig]:
    """Return the list of roles for a step (length 1 for single-role steps)."""
    if isinstance(step, MultiRoleStepConfig):
        return list(step.roles)
    return [step]


def _resolve_store_uri(nix_cfg: NixConfig, role_name: str) -> str:
    store = nix_cfg.store or _user_config.nix_store
    if not store:
        raise ValueError(
            f"role {role_name!r}: nix.store is not set and ~/.seekrchain.toml's "
            "`nix_store` is not configured. Set one or the other (e.g. "
            "nix_store = \"s3://bucket\")."
        )
    _validate_store_uri(store, role_name)
    return store


def _validate_store_uri(uri: str, role_name: str) -> None:
    """Reject store URIs that nix's native substituter can't handle.

    Specifically: nix's ``s3://`` store reads the netloc as the bucket name
    and ignores any path. Passing ``s3://bucket/prefix`` makes nix construct
    invalid AWS API calls (bucket name = ``"bucket/prefix"``, which the SDK
    rejects with InvalidBucketName). Other schemes (``http://``, ``file://``)
    handle paths normally; only check s3.

    Fails at submit time with a message that points at the right shape,
    rather than letting the in-cluster build step error out mid-workflow.
    """
    if not uri.startswith("s3://"):
        return

    from urllib.parse import urlparse

    parsed = urlparse(uri)
    # path is "" for s3://bucket?... and "/" for s3://bucket/?...; anything
    # else is a prefix that nix won't honor.
    if parsed.path and parsed.path not in ("", "/"):
        raise ValueError(
            f"role {role_name!r}: nix's s3:// store does not support path "
            f"prefixes. Got nix_store={uri!r}; expected "
            "s3://<bucket>[?region=...&endpoint=...]. "
            "If you need to share a bucket with other content, either give "
            "the nix cache its own bucket, or wait for the seekr-nix-cache "
            "daemon to be re-enabled (which adds prefix support via HTTP)."
        )


def _resolve_closure_path(nix_cfg: NixConfig, role_name: str) -> str:
    """Return the closure path, evaluating from `expression` if needed.

    Caches the result back into ``nix_cfg.closure`` so downstream code
    (jobset rendering) doesn't have to re-evaluate. Eval is pure but it's
    a subprocess; doing it once at the workflow level keeps things fast.
    """
    if nix_cfg.closure is not None:
        return nix_cfg.closure

    if nix_cfg.expression is None:
        # NixConfig's validator should have caught this, but defend in depth.
        raise ValueError(
            f"role {role_name!r}: nix has neither `expression` nor `closure`"
        )

    closure = nix_utils.eval_closure_path(
        nix_cfg.expression, attr=nix_cfg.attr, system=nix_cfg.system,
    )
    # Cache for downstream consumers (jobset rendering).
    nix_cfg.closure = closure
    return closure


# Build step's script. Runs inside the user's chain-entrypoint.sh wrapper,
# so it inherits PATH, env, working dir (/seekr-chain/workspace), and
# heartbeat/log-flushing. The script just needs to:
#   1. Configure nix.conf so it can both build *and* substitute deps from
#      the existing store (so unchanged transitive deps don't rebuild).
#   2. Run `nix build`.
#   3. Push the resulting closure with `nix copy --to`.
#
# Store + expected closure are passed via env vars (SEEKR_CHAIN_NIX_STORE,
# SEEKR_CHAIN_NIX_CLOSURE) rather than baked into the script. That lets
# `_detect_closure_hash` see the closure from the build step's role.env and
# attach the same `seekr-chain.nix/closure: <hash>` label that user (consumer)
# pods carry — so consumer pods' podAffinity preference targets the node
# where this build step ran (warm cache). The flake-ref pieces (expression,
# system, attr) are baked in because nothing else looks at them.
#
# We use `path:` flake refs unconditionally so the same code path handles
# both bare-flake.nix directories and the various subpath layouts users
# might have. For a classic .nix file (non-flake), auto-build isn't
# supported — the schema-level requirement should be flake-only at the
# resolution layer; we'll raise if we see that case below.
_BUILD_SCRIPT_TEMPLATE = """\
set -e

START_TIME=$(date +%s)
# stderr from nix build + nix copy is tee'd here so the final summary
# can parse stats out of it. /tmp is the build pod's ephemeral scratch.
LOG=/tmp/nix-build.log

# aws-sdk-cpp timeouts (s3 transport). 10 min per HTTP request gives
# ~3 MB/s threshold on multi-GB NARs — anything slower is stuck, not
# merely slow. 10s on connect catches DNS / TCP setup failures fast.
# nix's own stalled-download-timeout doesn't apply to s3 transport.
export AWS_REQUEST_TIMEOUT=600000
export AWS_CONNECT_TIMEOUT=10000

# `local?root=/nix-shared` as the FIRST substituter: when this pod lands
# on a node that already has paths cached on /var/lib/seekr-chain/nix
# (because a previous build/run pod populated it), nix sees the paths
# present in that local store and substitutes from local disk (~1 GB/s)
# instead of from s3 (~100 MB/s) or cache.nixos.org (~10 MB/s). This is
# distinct from setting `--store local?root=` (which redirects writes
# and has eval-store quirks) — substituters only control reads.
{{
  echo 'experimental-features = nix-command flakes'
  echo 'sandbox = false'
  echo 'filter-syscalls = false'
  echo "substituters = local?root=/nix-shared $SEEKR_CHAIN_NIX_STORE https://cache.nixos.org"
  # The local store has no signing key; tell nix to trust it without sigs.
  echo 'trusted-substituters = local?root=/nix-shared'
  echo 'require-sigs = false'
  # Default 10 lines is too few to debug failed builds. 200 captures
  # most autoconf/configure failures with their actual error context.
  echo 'log-lines = 200'
}} >> /etc/nix/nix.conf

cd /seekr-chain/workspace

# Build into the image's default /nix store. We tried using --store
# local?root=/nix-shared for the build itself, but flake source imports
# split between the eval store and the chroot store inconsistently —
# nix would copy the source to /nix-shared/nix/store/<hash>-source and
# then fail validation with "path is not in the Nix store". The build
# store is ephemeral in this pod anyway; what matters is that the
# *resulting closure* lands both on the node's hostPath volume (warm
# cache) and in $SEEKR_CHAIN_NIX_STORE (durable cache).
FLAKE_REF='path:{expression}#packages.{system}.{attr}'
echo "=== nix build $FLAKE_REF ==="
BUILT=$(nix build --print-out-paths --no-link "$FLAKE_REF" 2> >(tee -a "$LOG" >&2))
echo "built closure: $BUILT"

if [ "$BUILT" != "$SEEKR_CHAIN_NIX_CLOSURE" ]; then
    echo "FATAL: nix build produced $BUILT but submit-time eval expected $SEEKR_CHAIN_NIX_CLOSURE" >&2
    echo "this usually means the source tree drifted between submit and build" >&2
    exit 1
fi

# Mirror the closure to the node's hostPath. A consumer pod scheduled to
# the same node via closure-hash podAffinity will find /nix/store/<hash>
# already populated (via the subPath=nix mount) and chain-nix-init's
# `nix copy --from` will be a no-op.
echo "=== nix copy --to local?root=/nix-shared (warm cache for consumers) ==="
nix copy --to "local?root=/nix-shared" --no-check-sigs "$BUILT" 2> >(tee -a "$LOG" >&2)

# Durable copy: push to the configured binary cache. Compression scheme
# is configurable via user_config.nix_compression (default zstd). xz is
# ~5x slower and single-threaded; on a multi-GB closure that turns a
# 30s compress into 7min. The narinfo records each NAR's scheme, so
# consumers' `nix copy --from` decompresses correctly regardless of
# what we picked at upload time. Only NEW paths are affected — existing
# paths in the cache stay as they were written (mixed-scheme caches
# are fine in nix).
case "$SEEKR_CHAIN_NIX_STORE" in
  *\\?*) COPY_URI="$SEEKR_CHAIN_NIX_STORE&compression={compression}" ;;
  *)     COPY_URI="$SEEKR_CHAIN_NIX_STORE?compression={compression}" ;;
esac
echo "=== nix copy --to $COPY_URI ==="
nix copy --to "$COPY_URI" "$BUILT" 2> >(tee -a "$LOG" >&2)
echo "pushed $SEEKR_CHAIN_NIX_CLOSURE to $SEEKR_CHAIN_NIX_STORE"

# ─────────────────────────────────────────────────────────────────────
# Build summary — parsed from the captured log. The numbers here are
# what tells a docker-mode user "oh, this is the architectural win":
# how many paths reused from cache vs built from source, where they
# came from, and how much incremental data we actually shipped.
# ─────────────────────────────────────────────────────────────────────
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

# `grep -c` outputs "0" *and* exits 1 when there are no matches; `|| echo 0`
# would append a second "0", giving multi-line output. Use `|| true` + `${{:-0}}`.
SUBST_LOCAL=$(grep -c "from 'local'"                  "$LOG" 2>/dev/null || true)
SUBST_S3=$(grep -c    "from 's3://"                   "$LOG" 2>/dev/null || true)
SUBST_NIXOS=$(grep -c "from 'https://cache.nixos.org" "$LOG" 2>/dev/null || true)
SUBST_LOCAL=${{SUBST_LOCAL:-0}}; SUBST_S3=${{SUBST_S3:-0}}; SUBST_NIXOS=${{SUBST_NIXOS:-0}}
SUBST_TOTAL=$((SUBST_LOCAL + SUBST_S3 + SUBST_NIXOS))

BUILT_FROM_SRC=$(grep -oE "these [0-9]+ derivations? will be built" "$LOG" 2>/dev/null \\
                 | grep -oE '[0-9]+' | head -1)
BUILT_FROM_SRC=${{BUILT_FROM_SRC:-0}}

UPLOAD_PATHS=$(grep -c "^uploaded 's3://"    "$LOG" 2>/dev/null || true)
UPLOAD_PATHS=${{UPLOAD_PATHS:-0}}
UPLOAD_BYTES=$(grep -oE "uploaded '[^']+' \\([0-9]+ bytes\\)" "$LOG" 2>/dev/null \\
               | grep -oE '[0-9]+ bytes' | grep -oE '[0-9]+' \\
               | awk 'BEGIN {{s=0}} {{s+=$1}} END {{print s}}')
UPLOAD_BYTES=${{UPLOAD_BYTES:-0}}

CLOSURE_SIZE_BYTES=$(nix path-info --closure-size "$BUILT" 2>/dev/null \\
                     | awk '{{print $2+0}}')
CLOSURE_SIZE_BYTES=${{CLOSURE_SIZE_BYTES:-0}}
CLOSURE_PATHS=$(nix path-info --recursive "$BUILT" 2>/dev/null | wc -l || echo 0)

fmt_bytes() {{
  awk -v b="$1" 'BEGIN {{
    if (b >= 1073741824) printf "%.2f GB", b/1073741824
    else if (b >= 1048576) printf "%.2f MB", b/1048576
    else if (b >= 1024) printf "%.2f KB", b/1024
    else printf "%d B", b
  }}'
}}

if [ "$((SUBST_TOTAL + BUILT_FROM_SRC))" -gt 0 ]; then
  HIT_PCT=$(awk "BEGIN {{ printf \\"%.1f\\", 100 * $SUBST_TOTAL / ($SUBST_TOTAL + $BUILT_FROM_SRC) }}")
else
  HIT_PCT="n/a"
fi

cat <<EOF

===================================================================
  Nix build summary
===================================================================
  Total time:              ${{DURATION}}s
  Closure:                 $SEEKR_CHAIN_NIX_CLOSURE
  Closure size:            $(fmt_bytes "$CLOSURE_SIZE_BYTES")  ($CLOSURE_PATHS paths)

  Cache hits:              $SUBST_TOTAL paths  ($HIT_PCT%)
    from local hostPath:   $SUBST_LOCAL
    from $SEEKR_CHAIN_NIX_STORE:    $SUBST_S3
    from cache.nixos.org:  $SUBST_NIXOS
  Cache misses (built):    $BUILT_FROM_SRC paths

  Uploaded to cache:       $UPLOAD_PATHS paths,  $(fmt_bytes "$UPLOAD_BYTES")
===================================================================
EOF
"""

# Default if user_config.nix_compression isn't set. zstd: fast,
# multi-threaded, good compression ratio. See user_config.NixCompression
# for the full set of allowed values.
_DEFAULT_NIX_COMPRESSION = "zstd"


def _make_build_step(
    closure_path: str,
    nix_cfg: NixConfig,
    step_name: str,
    nix_runner_image: str,
    store_uri: str,
) -> SingleRoleStepConfig:
    """Create a synthetic build step that compiles + pushes one closure.

    The step is a regular image-mode step (nix-runner image, plain script);
    it intentionally does NOT use ``nix:`` mode itself — the whole point is
    that this step *creates* the closure, so closure-fetch semantics don't
    apply.
    """
    if nix_cfg.expression is None:
        # Only Mode-B steps reach here (Mode A has closure: explicit).
        # If a user provides only `closure:` and the closure is missing,
        # we can't build it — they have to pre-build manually.
        raise ValueError(
            f"closure {closure_path} is missing from store {store_uri} and "
            "only `nix.closure:` was provided (no `nix.expression:`). "
            "Auto-build needs the expression. Either pre-build and push "
            "manually, or specify `nix.expression`."
        )

    # nix's URI parameter is lowercase; user_config exposes the Literal
    # in uppercase per the seekr-chain convention for one-of options.
    compression = (
        _user_config.nix_compression or _DEFAULT_NIX_COMPRESSION.upper()
    ).lower()
    script = _BUILD_SCRIPT_TEMPLATE.format(
        expression=nix_cfg.expression,
        system=nix_cfg.system,
        attr=nix_cfg.attr,
        compression=compression,
    )

    return SingleRoleStepConfig(
        name=step_name,
        image=nix_runner_image,
        script=textwrap.dedent(script),
        resources=nix_cfg.build_resources or _DEFAULT_BUILD_RESOURCES,
        # Env carries the values the script reads + makes the closure hash
        # discoverable to _detect_closure_hash (which tags this pod with
        # `seekr-chain.nix/closure: <hash>` so consumer steps can prefer
        # the node that ran this build).
        env={
            "SEEKR_CHAIN_NIX_STORE": store_uri,
            "SEEKR_CHAIN_NIX_CLOSURE": closure_path,
        },
    )


def _build_step_name(closure_path: str) -> str:
    """Deterministic build-step name for a closure.

    Same closure -> same name -> single build step shared across all
    user steps that need it. Truncated to keep k8s name lengths sane
    (full hash is 32 chars; first 12 is plenty for dedup uniqueness).

    Name shape: ``nix-build-<hash[:12]>``. Argo / k8s reject names that
    start with non-alpha or contain underscores, so we use dashes
    throughout. The ``nix-build-`` prefix is enough to make the step
    visually distinguishable from user-authored steps.
    """
    return f"nix-build-{nix_utils.closure_hash_from_path(closure_path)[:12]}"


# Default runtime image for nix-mode roles. Built from `nix_poc/Dockerfile.nix-runner`.
# Bump the tag whenever the Dockerfile changes — k8s caches non-:latest tags
# per-node forever otherwise.
#
# Currently points at the Seekr-internal harbor mirror; we'll switch to a
# `ghcr.io/seekr-technologies/seekr-chain-nix-runner:...` image (analogous to
# the init image's ghcr.io default) before going public.
_DEFAULT_NIX_RUNNER_IMAGE = "harbor.ops-01.oci.int.seekr.com/k8-nexus/ntent/seekr-chain/nix-runner:v0.1.5"


def _get_nix_runner_image() -> str:
    """Resolve the nix-runner image; fall back to the hardcoded default.

    Same helper is re-exported from :mod:`seekr_chain.backends.argo.jobset`
    so render-time code doesn't have to reach across modules.
    """
    return _user_config.nix_runner_image or _DEFAULT_NIX_RUNNER_IMAGE


def resolve_nix_steps(config: WorkflowConfig) -> WorkflowConfig:
    """Walk a WorkflowConfig and augment it with build steps for missing closures.

    See module docstring. Mutates and returns ``config``.

    No-op when no step has ``nix:`` set — so this is safe to call
    unconditionally for every submit.
    """
    nix_roles_by_step: list[tuple] = []
    for step in config.steps:
        roles = _roles_of(step)
        nix_roles = [r for r in roles if r.nix is not None]
        if nix_roles:
            nix_roles_by_step.append((step, nix_roles))

    if not nix_roles_by_step:
        return config

    # Resolve closure paths for every nix role (eval if needed) and group
    # missing-but-needed-to-build closures by their path.
    nix_runner_image: Optional[str] = None
    needed_builds: dict[str, NixConfig] = {}  # closure_path -> representative NixConfig

    for step, nix_roles in nix_roles_by_step:
        for role in nix_roles:
            closure = _resolve_closure_path(role.nix, role.name or step.name)
            store_uri = _resolve_store_uri(role.nix, role.name or step.name)

            # Lazy-resolve the runner image only when we actually have a nix role.
            if nix_runner_image is None:
                nix_runner_image = _get_nix_runner_image()

            if nix_utils.closure_exists(store_uri, closure):
                logger.debug("nix closure %s already in %s", closure, store_uri)
                continue

            # Missing.
            if not role.nix.build:
                raise ValueError(
                    f"role {role.name or step.name!r}: closure {closure} is not in "
                    f"store {store_uri}, and nix.build=False. Either pre-build/push "
                    "it, set nix.build=True, or check the store URI."
                )

            # Mode B with auto-build: schedule one build step per unique closure.
            if closure not in needed_builds:
                needed_builds[closure] = role.nix
                logger.info(
                    "nix closure %s missing from %s — scheduling in-cluster build",
                    closure, store_uri,
                )

    if not needed_builds:
        return config

    # Synthesize the build steps.
    build_steps: list[SingleRoleStepConfig] = []
    closure_to_build_step_name: dict[str, str] = {}
    existing_step_names = {step.name for step in config.steps}

    for closure, repr_nix_cfg in needed_builds.items():
        name = _build_step_name(closure)
        if name in existing_step_names:
            # Pathological: user named a step like our build steps. Disambiguate
            # with a dash-suffix so the result stays DNS-label-safe.
            i = 1
            while f"{name}-{i}" in existing_step_names:
                i += 1
            name = f"{name}-{i}"
        existing_step_names.add(name)
        closure_to_build_step_name[closure] = name
        store_uri = _resolve_store_uri(repr_nix_cfg, name)
        build_steps.append(
            _make_build_step(
                closure_path=closure,
                nix_cfg=repr_nix_cfg,
                step_name=name,
                nix_runner_image=nix_runner_image,
                store_uri=store_uri,
            )
        )

    # Wire depends_on on user steps that need any of the built closures.
    for step, nix_roles in nix_roles_by_step:
        added_deps: list[str] = []
        for role in nix_roles:
            closure = role.nix.closure  # already cached by _resolve_closure_path
            if closure in closure_to_build_step_name:
                build_name = closure_to_build_step_name[closure]
                if build_name not in (step.depends_on or []) and build_name not in added_deps:
                    added_deps.append(build_name)
        if added_deps:
            step.depends_on = (step.depends_on or []) + added_deps

    # Insert build steps at the front of the workflow. The DAG ordering is
    # driven by depends_on, but front-of-list reads more naturally.
    config.steps = build_steps + list(config.steps)
    return config
