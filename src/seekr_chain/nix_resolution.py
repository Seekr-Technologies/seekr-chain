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


# Build step's script source lives at resources/nix-build.sh and gets
# uploaded with every job. The step invokes it via chain-entrypoint.sh
# (image-mode wrapper), and reads its config from these env vars set on
# the build step's container:
#   SEEKR_CHAIN_NIX_STORE       binary cache URI to push to
#   SEEKR_CHAIN_NIX_CLOSURE     expected /nix/store path
#   SEEKR_CHAIN_NIX_EXPRESSION  flake path inside /seekr-chain/workspace
#   SEEKR_CHAIN_NIX_SYSTEM      e.g. x86_64-linux
#   SEEKR_CHAIN_NIX_ATTR        attr inside the flake (default: "default")
#   SEEKR_CHAIN_NIX_COMPRESSION compression scheme for NAR uploads
#
# SEEKR_CHAIN_NIX_CLOSURE in env (not just script-baked) lets
# _detect_closure_hash see it on the build step's role.env and attach the
# `seekr-chain.nix/closure: <hash>` label so consumer pods' podAffinity
# preference targets the node that ran the build (warm cache).

# Default when user_config.nix_compression isn't set. zstd: fast,
# multi-threaded, good ratio. See user_config.NixCompression for allowed values.
_DEFAULT_NIX_COMPRESSION = "zstd"

_BUILD_SCRIPT_INVOCATION = "sh /seekr-chain/resources/nix-build.sh"


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

    return SingleRoleStepConfig(
        name=step_name,
        image=nix_runner_image,
        script=_BUILD_SCRIPT_INVOCATION,
        resources=nix_cfg.build_resources or _DEFAULT_BUILD_RESOURCES,
        # Env carries the values the script reads + makes the closure hash
        # discoverable to _detect_closure_hash (which tags this pod with
        # `seekr-chain.nix/closure: <hash>` so consumer steps can prefer
        # the node that ran this build).
        env={
            "SEEKR_CHAIN_NIX_STORE": store_uri,
            "SEEKR_CHAIN_NIX_CLOSURE": closure_path,
            "SEEKR_CHAIN_NIX_EXPRESSION": nix_cfg.expression,
            "SEEKR_CHAIN_NIX_SYSTEM": nix_cfg.system,
            "SEEKR_CHAIN_NIX_ATTR": nix_cfg.attr,
            "SEEKR_CHAIN_NIX_COMPRESSION": compression,
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


# Default runtime image for nix-mode roles. Built from
# `docker/Dockerfile.nix-runner` via the `Build Nix Runner Image`
# GitHub Actions workflow; the version pinned here must match the
# value in `docker/nix-runner.version`.
#
# Bump both files together whenever the Dockerfile changes — k8s
# caches non-:latest tags per-node forever otherwise, and the workflow
# refuses to overwrite an existing tag.
_DEFAULT_NIX_RUNNER_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-nix-runner:0.1.1@sha256:5058a650ca2f8c4ac5dde4eeb6ed13a4d7cd037ab886c4738e4502ed83490343"


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
