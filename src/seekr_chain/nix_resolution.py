"""Submit-time resolution of nix-mode steps in a WorkflowConfig.

Called from ``launch_argo_workflow`` after config validation, before manifest
rendering. Responsibilities:

1. For every role with ``nix.expression``, evaluate locally to compute the
   ``/nix/store/<hash>-<name>`` closure path. Eval requires ``nix`` on PATH;
   if it isn't, the error from :mod:`seekr_chain.nix_utils` is surfaced
   verbatim.

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
import os
from typing import Optional
from urllib.parse import urlparse

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

# Default runtime image for nix-mode roles. Built from
# `docker/Dockerfile.nix-runner` via the `Build Nix Runner Image`
# GitHub Actions workflow; the version pinned here must match the
# value in `docker/nix-runner.version`.
#
# Bump both files together whenever the Dockerfile changes — k8s
# caches non-:latest tags per-node forever otherwise, and the workflow
# refuses to overwrite an existing tag.
_DEFAULT_NIX_RUNNER_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-nix-runner:0.1.1@sha256:5058a650ca2f8c4ac5dde4eeb6ed13a4d7cd037ab886c4738e4502ed83490343"
_NIX_RUNNER_IMAGE = _user_config.nix_runner_image or _DEFAULT_NIX_RUNNER_IMAGE

# Default when user_config.nix_compression isn't set. zstd: fast,
# multi-threaded, good ratio. See user_config.NixCompression for allowed values.
_DEFAULT_NIX_COMPRESSION = "zstd"

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
_BUILD_SCRIPT_INVOCATION = "sh /seekr-chain/resources/nix-build.sh"

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


def _validate_expression_under_code_path(expression: str, code_path: str, role_name: str) -> str:
    """Validate that ``expression`` is a path inside ``code_path`` and return it.

    ``nix.expression`` is interpreted the same way at submit time (for local
    eval) and inside the build pod (for ``nix build path:./<expression>``
    from ``/seekr-chain/workspace``). That contract only holds if the
    expression points to a file that's part of the uploaded code bundle.

    Containment is checked lexically (``os.path.normpath``) so symlinks
    inside ``code_path`` that point outside the tree still work — they get
    dereferenced at upload time and land in the pod regardless of where
    their target lives. We only reject paths that *lexically* escape via
    ``..`` or absolute path components.
    """
    if os.path.isabs(expression):
        raise ValueError(
            f"role {role_name!r}: nix.expression must be a path relative to "
            f"code.path; got an absolute path {expression!r}. The build pod "
            "interprets the expression relative to /seekr-chain/workspace, so "
            "absolute submit-host paths don't translate."
        )

    code_root = os.path.normpath(code_path)
    joined = os.path.normpath(os.path.join(code_root, expression))
    if joined != code_root and not joined.startswith(code_root + os.sep):
        raise ValueError(
            f"role {role_name!r}: nix.expression={expression!r} escapes code.path "
            f"({code_path!r}). The flake must live inside the uploaded code "
            "bundle so the build pod can find it."
        )
    return joined


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

    if config.code is None or not config.code.path:
        raise ValueError(
            "nix-mode workflows require `code: {path: ...}` so the flake is "
            "uploaded with the job. The build pod runs `nix build` against "
            "/seekr-chain/workspace, which is populated from code.path."
        )

    role_to_closure, needed_builds = _collect_needed_builds(
        nix_roles_by_step, config.code.path, config.namespace or "argo",
    )
    if not needed_builds:
        return config

    return _inject_build_steps(config, nix_roles_by_step, role_to_closure, needed_builds)


def _collect_needed_builds(
    nix_roles_by_step: list[tuple],
    code_path: str,
    namespace: str,
) -> tuple[dict[int, str], dict[str, NixConfig]]:
    """Walk the nix-mode roles, eval each closure, and return:

    - ``role_to_closure``: id(role) -> resolved /nix/store path
    - ``needed_builds``: closure_path -> representative NixConfig for roles
      whose closure is missing from the store and need an auto-build

    Side effects on each role's NixConfig:

    - ``_resolved_closure`` cached so the jobset renderer doesn't re-eval.
    - ``_warm_nodes`` (exact-closure match) and ``_partial_warm_nodes``
      (any other closure on the node) populated via a single k8s API call
      per unique closure. The renderer injects both as soft nodeAffinity
      preferences (different weights).

    Raises if any role has ``build=False`` but the closure isn't in the store.
    """
    role_to_closure: dict[int, str] = {}
    needed_builds: dict[str, NixConfig] = {}
    # Dedup the warm-node query across roles in the same submit. One API
    # call per unique closure-hash, not per role. Each entry is
    # (exact_nodes, partial_nodes) from find_warm_nodes.
    warm_nodes_cache: dict[str, tuple[list[str], list[str]]] = {}

    for step, nix_roles in nix_roles_by_step:
        for role in nix_roles:
            role_name = role.name or step.name
            resolved_expression = _validate_expression_under_code_path(
                role.nix.expression, code_path, role_name,
            )
            closure = nix_utils.eval_closure_path(
                resolved_expression, attr=role.nix.attr, system=role.nix.system,
            )
            # Cache for downstream (jobset rendering) so we don't re-eval.
            role.nix._resolved_closure = closure
            role_to_closure[id(role)] = closure

            closure_hash = nix_utils.closure_hash_from_path(closure)
            if closure_hash not in warm_nodes_cache:
                warm_nodes_cache[closure_hash] = nix_utils.find_warm_nodes(
                    closure_hash, namespace=namespace,
                )
            exact_nodes, partial_nodes = warm_nodes_cache[closure_hash]
            role.nix._warm_nodes = exact_nodes
            role.nix._partial_warm_nodes = partial_nodes

            store_uri = _resolve_store_uri(role.nix, role_name)

            if nix_utils.closure_exists(store_uri, closure):
                logger.debug("nix closure %s already in %s", closure, store_uri)
                continue

            if not role.nix.build:
                raise ValueError(
                    f"role {role_name!r}: closure {closure} is not in "
                    f"store {store_uri}, and nix.build=False. Either pre-build/push "
                    "it, set nix.build=True, or check the store URI."
                )

            # Schedule one build step per unique closure.
            if closure not in needed_builds:
                needed_builds[closure] = role.nix
                logger.info(
                    "nix closure %s missing from %s — scheduling in-cluster build",
                    closure, store_uri,
                )

    return role_to_closure, needed_builds


def _inject_build_steps(
    config: WorkflowConfig,
    nix_roles_by_step: list[tuple],
    role_to_closure: dict[int, str],
    needed_builds: dict[str, NixConfig],
) -> WorkflowConfig:
    """Synthesize build steps for every entry in ``needed_builds``, then wire
    each affected user step's ``depends_on`` to the matching build step.
    Build steps are prepended to ``config.steps``.
    """
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
                nix_runner_image=_NIX_RUNNER_IMAGE,
                store_uri=store_uri,
            )
        )

    # Wire depends_on on user steps that need any of the built closures.
    for step, nix_roles in nix_roles_by_step:
        added_deps: list[str] = []
        for role in nix_roles:
            closure = role_to_closure[id(role)]
            if closure in closure_to_build_step_name:
                build_name = closure_to_build_step_name[closure]
                if build_name not in (step.depends_on or []) and build_name not in added_deps:
                    added_deps.append(build_name)
        if added_deps:
            step.depends_on = (step.depends_on or []) + added_deps

    # Build steps go at the front for readability — depends_on drives execution.
    config.steps = build_steps + list(config.steps)
    return config
