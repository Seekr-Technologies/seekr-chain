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

import datetime
import hashlib
import logging
import os
import time
from pathlib import Path
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
_DEFAULT_NIX_RUNNER_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-nix-runner:0.3.0@sha256:b26c9e5ff6ebb904abcd9e452c3c4bdf8ff0bf45a7d7a942eaeb221447ff2ede"
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
#   SEEKR_CHAIN_NIX_WORKSPACE   copied nix source tree inside /seekr-chain
#   SEEKR_CHAIN_NIX_EXPRESSION  flake path inside that nix workspace
#   SEEKR_CHAIN_NIX_SYSTEM      e.g. x86_64-linux
#   SEEKR_CHAIN_NIX_ATTR        attr inside the flake (default: "default")
#   SEEKR_CHAIN_NIX_COMPRESSION compression scheme for NAR uploads
#   SEEKR_NIX_STORE_BACKEND     same URI as SEEKR_CHAIN_NIX_STORE, under
#                               the name seekr-nix's nix-wrapper.sh shim
#                               requires to accelerate `nix build` at
#                               all -- without it every build falls
#                               through to unaccelerated real nix
#                               regardless of argv shape.
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


# Label every nix-mode pod (consumer or build) carries: identifies the
# closure that pod fetched/produced. Used both by the rendered podAffinity
# (concurrent co-scheduling) and by find_warm_nodes() below.
NIX_CLOSURE_LABEL = "seekr-chain.nix/closure"


def find_warm_nodes(
    closure_hash: str,
    namespace: str,
    limit: int = 10,
) -> list[str]:
    """Return exact warm-cache node names for a closure.

    One k8s API call, narrowed to an equality selector
    (``NIX_CLOSURE_LABEL == closure_hash``) — the API server does the
    filtering, so only nodes that actually have this closure come back.
    The closure literally lives at ``<hostpath>/nix/store/<hash>-...`` on
    each returned node (surfaced at ``/nix/store/<hash>-...`` inside any
    pod mounting it with ``subPath=nix``) — substituters hit local disk
    for the full closure on next consume. Rendered as a nodeAffinity
    preference (weight 90 in jobset.py).

    Recency ordering uses the most-recent pod's ``creation_timestamp``
    per node; the result is capped at ``limit``.

    Returns ``[]`` on any error (kubeconfig not set, RBAC denied, network
    unreachable, …). Warm-cache is a soft hint; a missing one means the
    scheduler falls back to a cold pull. Never raises.
    """
    started = time.perf_counter()
    try:
        from seekr_chain import k8s_utils
    except ImportError:
        return []

    try:
        v1 = k8s_utils.get_core_v1_api()
        result = v1.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"{NIX_CLOSURE_LABEL}={closure_hash}",
        )
    except Exception as e:
        logger.warning(
            "could not query warm nodes for closure %s in %s: %s; scheduler will pick without warm-cache hint",
            closure_hash,
            namespace,
            e,
        )
        return []

    try:
        pods = [p for p in result.items if p.spec.node_name]
        # A bare int 0 fallback would raise TypeError when sorted against
        # real datetimes, so the fallback must be datetime-typed too. If a
        # mix of aware/naive datetimes still slips through (e.g. a
        # partially-populated object from a flaky watch), the outer
        # try/except below catches the resulting TypeError and degrades to
        # [] rather than propagating it.
        pods.sort(
            key=lambda p: p.metadata.creation_timestamp or datetime.datetime.min,
            reverse=True,
        )

        nodes: list[str] = []
        seen: set[str] = set()
        for p in pods:
            node = p.spec.node_name
            if node not in seen and len(nodes) < limit:
                nodes.append(node)
                seen.add(node)
        logger.info(
            "Warm-node lookup for closure %s in namespace %s completed in %.3fs (nodes=%d)",
            closure_hash,
            namespace,
            time.perf_counter() - started,
            len(nodes),
        )
        return nodes
    except Exception as e:
        logger.warning(
            "error partitioning warm nodes for closure %s in %s: %s; scheduler will pick without warm-cache hint",
            closure_hash,
            namespace,
            e,
        )
        return []


def _resolve_store_uri(nix_cfg: NixConfig, role_name: str) -> str:
    store = nix_cfg.store or _user_config.nix_store
    if not store:
        raise ValueError(
            f"role {role_name!r}: nix.store is not set and ~/.seekrchain.toml's "
            "`nix_store` is not configured. Set one or the other (e.g. "
            'nix_store = "s3://bucket").'
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
    compression = (_user_config.nix_compression or _DEFAULT_NIX_COMPRESSION.upper()).lower()

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
            **(
                {"SEEKR_CHAIN_NIX_WORKSPACE": f"/seekr-chain/{nix_cfg._source_subdir}"}
                if nix_cfg._source_subdir is not None
                else {}
            ),
            "SEEKR_CHAIN_NIX_EXPRESSION": nix_cfg.expression,
            "SEEKR_CHAIN_NIX_SYSTEM": nix_cfg.system,
            "SEEKR_CHAIN_NIX_ATTR": nix_cfg.attr,
            "SEEKR_CHAIN_NIX_COMPRESSION": compression,
            # seekr-nix's nix-wrapper.sh shim only accelerates a `nix
            # build` call when this env var is set (its own generic,
            # runner-image-level convention for "an OCI backend is
            # configured" -- also read directly by `seekr-nix`'s own
            # CLI via ConnArgs). Without it, nix-build.sh's `nix build
            # --print-out-paths --no-link -L <flake-ref>` always falls
            # through to unaccelerated real nix, regardless of argv
            # shape -- confirmed directly: classify_build recognizes
            # this exact invocation correctly, but seekr_nix.rs's
            # `ShimAction::Build` handler exits NOT_ACCELERATED_EXIT
            # immediately if this specific var is unset, before ever
            # reaching our own OCI cache. Same store as
            # SEEKR_CHAIN_NIX_STORE -- just under the name seekr-nix's
            # generic convention expects.
            #
            # Contract: set unconditionally here (not gated on any
            # config flag) because `_make_build_step` is only ever
            # called for nix-mode roles, which always run the
            # shim-aware seekr-nix-runner image (`_NIX_RUNNER_IMAGE`
            # above) -- there's no code path where this function runs
            # against an image that doesn't understand this var. If
            # `_make_build_step` ever gets called for a non-nix-runner
            # image, this line needs to become conditional.
            "SEEKR_NIX_STORE_BACKEND": store_uri,
        },
    )


def _build_step_name(closure_path: str, store_uri: str) -> str:
    """Deterministic build-step name for a (closure, store) pair.

    Same closure + same store -> same name -> single build step shared
    across all user steps that need it. Two roles sharing a closure but
    pointed at different stores get distinct names, since each needs its
    own build step pushing to its own store. Truncated to keep k8s name
    lengths sane (full hash is 32 chars; first 12 is plenty for dedup
    uniqueness; the store hash only needs to disambiguate, so 8 is enough).

    Name shape: ``nix-build-<closure_hash[:12]>-<store_hash[:8]>``. Argo /
    k8s reject names that start with non-alpha or contain underscores, so
    we use dashes throughout. The ``nix-build-`` prefix is enough to make
    the step visually distinguishable from user-authored steps.
    """
    closure_hash = nix_utils.closure_hash_from_path(closure_path)[:12]
    store_hash = hashlib.sha256(store_uri.encode()).hexdigest()[:8]
    return f"nix-build-{closure_hash}-{store_hash}"


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


def _collect_nix_roles_by_step(config: WorkflowConfig) -> list[tuple]:
    """Return [(step, [nix-mode roles on that step]), ...] for every step
    that has at least one nix-mode role."""
    nix_roles_by_step: list[tuple] = []
    for step in config.steps:
        roles = _roles_of(step)
        nix_roles = [r for r in roles if r.nix is not None]
        if nix_roles:
            nix_roles_by_step.append((step, nix_roles))
    return nix_roles_by_step


def resolve_nix_steps(config: WorkflowConfig, staged_code_dir: str, staging_dir: str | Path) -> WorkflowConfig:
    """Walk a WorkflowConfig and augment it with build steps for missing closures.

    See module docstring. Mutates and returns ``config``.

    No-op when no step has ``nix:`` set — so this is safe to call
    unconditionally for every submit.

    ``staged_code_dir`` is the caller's staged user workspace (typically the
    cheap symlink tree). ``staging_dir`` is where a cache-miss materializes
    a role's copied nix source tree (``nix-workspaces/<digest>/workspace``),
    already the location asset packaging tars up.
    """
    nix_roles_by_step = _collect_nix_roles_by_step(config)

    if not nix_roles_by_step:
        return config

    if config.code is None or not config.code.path:
        raise ValueError(
            "nix-mode workflows require `code: {path: ...}` so the flake is "
            "uploaded with the job. The build pod runs `nix build` against "
            "/seekr-chain/workspace, which is populated from code.path."
        )

    role_to_key, needed_builds = _collect_needed_builds(
        nix_roles_by_step,
        config.code.path,
        config.code.include or [],
        config.code.exclude or [],
        str(staged_code_dir),
        staging_dir,
        config.namespace or "argo",
    )
    if not needed_builds:
        return config

    return _inject_build_steps(config, nix_roles_by_step, role_to_key, needed_builds)


def _collect_needed_builds(
    nix_roles_by_step: list[tuple],
    code_path: str,
    code_include: list[str],
    code_exclude: list[str],
    staged_root: str,
    staging_dir: str | Path,
    namespace: str,
) -> tuple[dict[int, tuple[str, str]], dict[tuple[str, str], NixConfig]]:
    """Walk the nix-mode roles, eval each closure, and return:

    ``code_path`` is the live directory (used for the lexical containment
    check); ``staged_root`` is the general staged user workspace used as a
    fallback when a role has no dedicated nix source tree.

    - ``role_to_key``: id(role) -> (resolved /nix/store path, store_uri).
      Store is part of the key because two roles can share a closure while
      being configured with different ``nix.store`` values — each role must
      depend on the build step that actually pushes to *its* store.
    - ``needed_builds``: (closure_path, store_uri) -> representative NixConfig
      for roles whose closure is missing from that store and need an
      auto-build

    Side effects on each role's NixConfig:

    - ``_resolved_closure`` cached so the jobset renderer doesn't re-eval.
    - ``_warm_nodes`` (exact-closure match) populated via a single k8s API
      call per unique closure. The renderer injects it as a soft
      nodeAffinity preference.

    Raises if any role has ``build=False`` but the closure isn't in the store.
    """
    role_to_key: dict[int, tuple[str, str]] = {}
    needed_builds: dict[tuple[str, str], NixConfig] = {}
    # Dedup the warm-node query across roles in the same submit. One API
    # call per unique closure-hash, not per role.
    warm_nodes_cache: dict[str, list[str]] = {}

    for step, nix_roles in nix_roles_by_step:
        for role in nix_roles:
            role_name = role.name or step.name
            resolved_expression = _validate_expression_under_code_path(
                role.nix.expression,
                code_path,
                role_name,
            )
            logger.info(
                "Resolving nix closure for role %r from staged upload set (expression=%r)",
                role_name,
                role.nix.expression,
            )
            include = role.nix.include if role.nix.include is not None else code_include
            exclude = role.nix.exclude if role.nix.exclude is not None else code_exclude
            store_uri = _resolve_store_uri(role.nix, role_name)
            eval_result = nix_utils.maybe_eval_closure(
                code_path=code_path,
                staged_root=staged_root,
                staging_dir=staging_dir,
                resolved_expression=resolved_expression,
                role_name=role_name,
                expression=role.nix.expression,
                attr=role.nix.attr,
                system=role.nix.system,
                source_include=include,
                source_exclude=exclude,
                store_uri=store_uri,
            )
            closure = eval_result.closure_path
            role.nix._source_digest = eval_result.source_digest
            role.nix._source_subdir = eval_result.source_subdir
            role.nix._staged_source_dir = str(eval_result.staged_source_dir) if eval_result.staged_source_dir else None
            # Cache for downstream (jobset rendering) so we don't re-eval.
            role.nix._resolved_closure = closure

            closure_hash = nix_utils.closure_hash_from_path(closure)
            if closure_hash not in warm_nodes_cache:
                warm_nodes_cache[closure_hash] = find_warm_nodes(
                    closure_hash,
                    namespace=namespace,
                )
            role.nix._warm_nodes = warm_nodes_cache[closure_hash]

            # Store is part of the key: two roles can share a closure but
            # be configured with different nix.store values, and each
            # needs its own build step pushing to its own store.
            key = (closure, store_uri)
            role_to_key[id(role)] = key

            if eval_result.closure_in_store:
                logger.debug("nix closure %s already in %s", closure, store_uri)
                continue

            if not role.nix.build:
                raise ValueError(
                    f"role {role_name!r}: closure {closure} is not in "
                    f"store {store_uri}, and nix.build=False. Either pre-build/push "
                    "it, set nix.build=True, or check the store URI."
                )

            # Schedule one build step per unique (closure, store) pair.
            if key not in needed_builds:
                needed_builds[key] = role.nix
                logger.info(
                    "nix closure %s missing from %s — scheduling in-cluster build",
                    closure,
                    store_uri,
                )

    return role_to_key, needed_builds


def _inject_build_steps(
    config: WorkflowConfig,
    nix_roles_by_step: list[tuple],
    role_to_key: dict[int, tuple[str, str]],
    needed_builds: dict[tuple[str, str], NixConfig],
) -> WorkflowConfig:
    """Synthesize build steps for every entry in ``needed_builds``, then wire
    each affected user step's ``depends_on`` to the matching build step.
    Build steps are prepended to ``config.steps``.
    """
    build_steps: list[SingleRoleStepConfig] = []
    key_to_build_step_name: dict[tuple[str, str], str] = {}
    existing_step_names = {step.name for step in config.steps}

    for (closure, store_uri), repr_nix_cfg in needed_builds.items():
        name = _build_step_name(closure, store_uri)
        if name in existing_step_names:
            # Pathological: user named a step like our build steps. Disambiguate
            # with a dash-suffix so the result stays DNS-label-safe.
            i = 1
            while f"{name}-{i}" in existing_step_names:
                i += 1
            name = f"{name}-{i}"
        existing_step_names.add(name)
        key_to_build_step_name[(closure, store_uri)] = name
        build_steps.append(
            _make_build_step(
                closure_path=closure,
                nix_cfg=repr_nix_cfg,
                step_name=name,
                nix_runner_image=_NIX_RUNNER_IMAGE,
                store_uri=store_uri,
            )
        )

    # Wire depends_on on user steps that need any of the built closures,
    # matching on (closure, store) so each role depends on the build step
    # that pushes to *its* store, not just any step that built the closure.
    for step, nix_roles in nix_roles_by_step:
        added_deps: list[str] = []
        for role in nix_roles:
            key = role_to_key[id(role)]
            if key in key_to_build_step_name:
                build_name = key_to_build_step_name[key]
                if build_name not in (step.depends_on or []) and build_name not in added_deps:
                    added_deps.append(build_name)
        if added_deps:
            step.depends_on = (step.depends_on or []) + added_deps

    # Build steps go at the front for readability — depends_on drives execution.
    config.steps = build_steps + list(config.steps)
    return config


def process_nix(config: WorkflowConfig, *, staged_code_dir: str, staging_dir: Path) -> WorkflowConfig:
    """Resolve nix-mode steps end to end: eval/build closures, inject build
    steps and warm-node affinity, and materialize any missing nix source
    trees directly into ``staging_dir`` for upload. No-op when ``config``
    has no nix-mode roles.
    """
    return resolve_nix_steps(config, staged_code_dir=staged_code_dir, staging_dir=staging_dir)
