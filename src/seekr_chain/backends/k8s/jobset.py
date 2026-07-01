#!/usr/bin/env python3

import copy
import json
import logging
import os
import textwrap
from pathlib import Path
from typing import Optional

from seekr_chain import constants, k8s_utils, nix_utils, s3_utils
from seekr_chain.config import (
    SingleRoleStepConfig,
    StepConfig,
    WorkflowConfig,
)
from seekr_chain.nix_resolution import (
    _NIX_RUNNER_IMAGE,
    _resolve_store_uri,
)
from seekr_chain.user_config import config as _user_config
from seekr_chain.utils import format_bytes, resolve_image

logger = logging.getLogger(__name__)

_DEFAULT_INIT_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-init:1.0.0@sha256:f1fc456cffae92eab86c18814f3668c766ab12b69231308083ff128a8d4d0a9c"
_INIT_IMAGE = _user_config.init_image or _DEFAULT_INIT_IMAGE


_DEFAULT_NIX_STORE_VOLUME_KIND = "hostPath"
_DEFAULT_NIX_STORE_HOSTPATH = "/var/lib/seekr-chain/nix"
_DEFAULT_NIX_STORE_MAX_BYTES = 128 * 1024**3  # GiB


def _parse_size_to_bytes(s: str) -> int:
    """Parse "50G" / "50GiB" / "1024" → integer bytes. Case-insensitive.

    Accepts both SI (1000-based) and IEC (1024-based) suffixes; for v1 we
    treat both as 1024-based — it's a soft limit on disk and the difference
    isn't material.
    """
    s = s.strip().upper().rstrip("B").rstrip("I")
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    suffix = ""
    for k in multipliers:
        if k and s.endswith(k):
            suffix = k
            s = s[: -len(k)]
            break
    return int(float(s) * multipliers[suffix])


# Script source for the chain-nix-init init container lives at
# resources/chain-nix-init.sh and gets uploaded with every job (via
# launch_argo_workflow's resources copy). The init container invokes it as
# `sh /seekr-chain/resources/chain-nix-init.sh` after chain-init has
# downloaded resources to /seekr-chain.


# main container's step_args for nix-mode roles. The closure-fetch happened
# in chain-nix-init; main exports the closure on PATH + LD_LIBRARY_PATH and
# runs the normal entrypoint.
#
# Why LD_LIBRARY_PATH: nixpkgs-built libraries often dlopen each other
# without sufficient RUNPATH (e.g. RCCL dlopens `libibverbs.so.1` and
# `libamdhip64.so`). When the RUNPATH doesn't include the closure's lib
# dir, dlopen falls back to the system search path and fails. The
# buildEnv merges all input pkgs' /lib into <closure>/lib, so prepending
# that dir to LD_LIBRARY_PATH makes the runtime dlopens succeed —
# unlocking RDMA in multi-node training, among other things.
_NIX_MAIN_STEP_ARGS = (
    'export PATH="$SEEKR_CHAIN_NIX_CLOSURE/bin:$PATH"; '
    'export LD_LIBRARY_PATH="$SEEKR_CHAIN_NIX_CLOSURE/lib:${{LD_LIBRARY_PATH:-}}"; '
    "exec {entrypoint}"
)


def _eval_role_closure(nix_cfg, code_path: str | None):
    """Eval the closure path for a nix role, resolving expression vs code.path.

    ``nix.expression`` is interpreted relative to ``code.path``; both submit-
    time eval and the build pod's ``cd workspace; nix build path:./...`` agree
    on this contract (see ``_validate_expression_under_code_path``).

    Returns the cached ``nix_cfg._resolved_closure`` if ``resolve_nix_steps``
    already evaluated it during the submit-time pre-pass — each `nix eval`
    is a ~1.5s subprocess even when nix's internal cache is hot, so repeating
    it 3x per submit (render-time on every role + closure-hash detection) is
    a real cost. Falls through to a fresh eval when the cache is unset
    (unit tests that bypass resolve_nix_steps).

    ``code_path`` may be None in unit tests that render in isolation and mock
    eval_closure_path; in that case we pass the raw expression through.
    """
    if nix_cfg._resolved_closure is not None:
        return nix_cfg._resolved_closure

    if code_path:
        full = os.path.normpath(os.path.join(code_path, nix_cfg.expression))
    else:
        full = nix_cfg.expression
    return nix_utils.eval_closure_path(full, attr=nix_cfg.attr, system=nix_cfg.system)


def _resolve_nix_role(role_config, code_path: str | None = None) -> dict:
    """For a role with ``nix:`` set, compute everything the template needs to
    render the chain-nix-init init container + the simplified main container.

    Returns a dict with these keys:

    ``image``
        nix-runner OCI image reference (also used for chain-nix-init).
    ``closure``
        ``/nix/store/<hash>-<name>`` path.
    ``closure_hash``
        32-char hash, used for the pod label + the closure-affinity term.
    ``store_uri``
        URI for the binary cache (s3://bucket, etc. — any nix store type).
    ``init_env``
        Env entries (name+value) for chain-nix-init. Just the nix-mode pair;
        the template adds AWS creds and other secret refs from the workflow.
    ``main_env``
        Env entries (name+value) for the main container so user scripts can
        introspect what closure they're running.
    ``volume_kind``
        ``"hostPath"`` or ``"emptyDir"``. Controls the nix-store volume shape.
    ``hostpath``
        Host filesystem path used when ``volume_kind == "hostPath"``.
    """

    nix = role_config.nix

    # Resolve store first: it's a cheap dict lookup and fails the user the
    # fastest. Eval is a subprocess that takes ~100ms; no point doing it
    # only to discover the store wasn't configured.
    store_uri = _resolve_store_uri(nix, role_config.name or "")

    # Eval requires nix on the local PATH; the error from nix_utils is
    # actionable enough — surface it directly. resolve_nix_steps (called
    # earlier in the submit path) will already have run eval once; nix's
    # internal eval store makes the repeat call cheap.
    closure = _eval_role_closure(nix, code_path)
    closure_hash = nix_utils.closure_hash_from_path(closure)

    # build=False sanity check at render time. resolve_nix_steps (called
    # earlier in the submit path) is the canonical authority for "this
    # closure is in the store, or we've scheduled a build step that will
    # produce it." With build=True we trust that scheduling; with
    # build=False there's no build step, so re-confirm here as a fast
    # failure rather than waiting for the pod's nix copy --from to 404.
    if not nix.build and not nix_utils.closure_exists(store_uri, closure):
        raise ValueError(
            f"role {role_config.name!r}: closure {closure} is not in store "
            f"{store_uri}, and nix.build=False. Either pre-build/push it, "
            "set nix.build=True, or check the store URI."
        )

    volume_kind = _user_config.nix_store_volume_kind or _DEFAULT_NIX_STORE_VOLUME_KIND
    if volume_kind not in ("hostPath", "emptyDir"):
        raise ValueError(
            f"nix_store_volume_kind must be 'hostPath' or 'emptyDir'; got "
            f"{volume_kind!r}. Set ~/.seekrchain.toml's nix_store_volume_kind."
        )
    hostpath = _user_config.nix_store_hostpath or _DEFAULT_NIX_STORE_HOSTPATH

    return {
        "image": _NIX_RUNNER_IMAGE,
        "closure": closure,
        "closure_hash": closure_hash,
        "store_uri": store_uri,
        "init_env": [
            {"name": "SEEKR_CHAIN_NIX_STORE", "value": store_uri},
            {"name": "SEEKR_CHAIN_NIX_CLOSURE", "value": closure},
            {
                "name": "SEEKR_CHAIN_NIX_STORE_MAX_BYTES",
                "value": str(
                    _parse_size_to_bytes(_user_config.nix_store_max_size)
                    if _user_config.nix_store_max_size
                    else _DEFAULT_NIX_STORE_MAX_BYTES
                ),
            },
        ],
        "main_env": [
            {"name": "SEEKR_CHAIN_NIX_STORE", "value": store_uri},
            {"name": "SEEKR_CHAIN_NIX_CLOSURE", "value": closure},
            # Point common TLS clients at the closure's CA bundle. nss-cacert
            # is a transitive dep of anything that does HTTPS (Python requests,
            # urllib, pip, curl, wget, ...), so this path exists in essentially
            # any non-trivial closure. We set three env vars to cover the
            # mainstream lookup paths:
            #   SSL_CERT_FILE        — Python's stdlib `ssl` module
            #   REQUESTS_CA_BUNDLE   — requests' Session.merge_environment_settings
            #   NIX_SSL_CERT_FILE    — nixpkgs-patched curl / openssl / nss
            # Users with closures missing cacert can override via their step's env:.
            *[
                {"name": k, "value": f"{closure}/etc/ssl/certs/ca-bundle.crt"}
                for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NIX_SSL_CERT_FILE")
            ],
        ],
        "volume_kind": volume_kind,
        "hostpath": hostpath,
    }


def _get_pvcs(config) -> tuple[list, list]:
    """
    Get PVC volume and mounts for a job config
    """
    vols, mounts = [], []
    # User-defined volumes
    if config.resources.persistent_volume_claims:
        for claim in config.resources.persistent_volume_claims:
            vols.append({"name": claim.name, "persistentVolumeClaim": {"claimName": claim.name}})
            mounts.append({"name": claim.name, "mountPath": claim.mount_path})
    return vols, mounts


def _get_env(
    workflow_config: WorkflowConfig,
    step_config,
    workflow_secrets: list[dict],
    master_addr: str,
    workflow_name: str,
    jobset_name: str,
) -> list[dict]:
    env_dict = {}
    if workflow_config.env:
        env_dict = {**env_dict, **workflow_config.env}
    if step_config.env:
        env_dict = {**env_dict, **step_config.env}

    # Env set by us
    env = [
        {
            "name": "NODE_RANK",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.annotations['jobset.sigs.k8s.io/job-index']"}},
        },
        {
            "name": "RESTART_ATTEMPT",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.annotations['jobset.sigs.k8s.io/restart-attempt']"}},
        },
        {"name": "NNODES", "value": str(step_config.resources.num_nodes)},
        {
            "name": "MASTER_ADDR",
            "value": master_addr,
        },
        {"name": "MASTER_PORT", "value": "29500"},
        {"name": "HOSTFILE", "value": constants.HOSTFILE_PATH},
        {"name": "PEERMAP", "value": constants.PEERMAP_PATH},
        {"name": "GPUS_PER_NODE", "value": str(step_config.resources.gpus_per_node)},
        {"name": "SEEKR_CHAIN_WORKFLOW_ID", "value": workflow_name},
        {"name": "SEEKR_CHAIN_JOBSET_ID", "value": jobset_name},
        {
            "name": "SEEKR_CHAIN_POD_ID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}},
        },
        {"name": "SEEKR_CHAIN_POD_INSTANCE_ID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "SEEKR_CHAIN_ARGS", "value": constants.ARGS_PATH},
        {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
    ] + workflow_secrets

    existing_keys = set(item["name"] for item in env)

    # Combine
    env = env + [{"name": k, "value": v} for k, v in env_dict.items() if k not in existing_keys]

    return env


def _construct_hostfile(
    js_name: str,
    js_pod_name: str,
    subdomain: str,
    role_config,
    assets_path,
    step_name,
):
    """
    Construct and write hostfile for this jobset
    """

    lines = []
    for i in range(role_config.resources.num_nodes):
        lines += [f"{js_name}-{js_pod_name}-{i}-0.{subdomain} slots={role_config.resources.gpus_per_node}\n"]

    role_path = _generate_role_asset_path(step_name=step_name, role_name=role_config.name, parent=assets_path)
    role_path.mkdir(exist_ok=True, parents=True)
    hostfile_path = role_path / "hostfile"
    with open(hostfile_path, "w") as f:
        f.writelines(lines)


def _generate_role_asset_path(step_name: str, role_name: str, parent: Optional[Path | str] = None) -> Path | str:
    out = f"step={step_name}"
    if role_name:
        out += f"/role={role_name}"

    if parent:
        if isinstance(parent, Path):
            out = parent / out
        else:
            out = parent.rstrip("/") + "/" + out
    return out


def _normalize_env(env_list: list[dict]) -> list[dict]:
    """Normalize env var dicts to a flat form suitable for the Jinja2 template.

    Each entry becomes one of three shapes:
      {"name": ..., "kind": "value",       "value": str}
      {"name": ..., "kind": "fieldRef",    "fieldPath": str}
      {"name": ..., "kind": "secretKeyRef","secret_name": str, "key": str, "optional": bool}
    """
    result = []
    for e in env_list:
        if "value" in e:
            result.append({"name": e["name"], "kind": "value", "value": str(e["value"])})
        elif "valueFrom" in e:
            vf = e["valueFrom"]
            if "fieldRef" in vf:
                result.append({"name": e["name"], "kind": "fieldRef", "fieldPath": vf["fieldRef"]["fieldPath"]})
            elif "secretKeyRef" in vf:
                skr = vf["secretKeyRef"]
                result.append(
                    {
                        "name": e["name"],
                        "kind": "secretKeyRef",
                        "secret_name": skr["name"],
                        "key": skr["key"],
                        "optional": skr.get("optional", False),
                    }
                )
    return result


def _detect_closure_hash(role_config, code_path: str | None = None) -> str | None:
    """Return the closure hash this role is associated with, or None.

    Two sources:
    - Nix-mode roles: evaluated from ``role.nix.expression`` (resolved against
      ``code_path``). nix's internal eval store makes the repeat call cheap.
    - Auto-injected build steps carry the closure via the
      ``SEEKR_CHAIN_NIX_CLOSURE`` env var (set by ``nix_resolution`` when
      synthesizing the build step).

    The hash drives two things on the rendered pod:
    - ``seekr-chain.nix/closure`` label, so other pods needing the same
      closure can target this pod's node.
    - A ``podAffinity`` preference (in :func:`_build_role_context`) on that
      same label, so consumers prefer nodes where producers ran.
    """
    if role_config.nix is not None:
        closure = _eval_role_closure(role_config.nix, code_path)
        return nix_utils.closure_hash_from_path(closure)
    env = role_config.env or {}
    closure_path = env.get("SEEKR_CHAIN_NIX_CLOSURE")
    if closure_path:
        return nix_utils.closure_hash_from_path(closure_path)
    return None


def _detect_warm_nodes(role_config) -> list[str]:
    """Return the warm node names this role should prefer, or [].

    Populated by ``resolve_nix_steps`` (the submit-time pass) for nix-mode
    roles via :func:`nix_utils.find_warm_nodes`. Build steps (image-mode
    with SEEKR_CHAIN_NIX_CLOSURE in env) get [] — they're producers, not
    consumers, so they don't benefit from steering toward existing warm
    nodes.

    Returns [] when resolution didn't run (unit tests that bypass
    resolve_nix_steps), which keeps nodeAffinity injection a no-op.
    """
    if role_config.nix is not None and role_config.nix._warm_nodes:
        return role_config.nix._warm_nodes
    return []


def _detect_partial_warm_nodes(role_config) -> list[str]:
    """Return the partial warm node names this role should prefer, or [].

    These are nodes that have pulled *some other* closure (any non-matching
    value of the closure label). Their /nix-shared share a chunk of paths
    with this closure (glibc, gcc, bash, …); steering toward them yields a
    weaker but real warm-cache benefit when no exact-match node exists.
    """
    if role_config.nix is not None and role_config.nix._partial_warm_nodes:
        return role_config.nix._partial_warm_nodes
    return []


def _merge_affinity_with_closure(
    base_affinity: dict | None,
    closure_hash: str | None,
    warm_nodes: list[str] | None = None,
    partial_warm_nodes: list[str] | None = None,
) -> dict | None:
    """Combine the workflow-level affinity with closure warm-cache hints.

    Three terms get added, all soft (``preferredDuringSchedulingIgnoredDuringExecution``):

    1. **podAffinity** on ``seekr-chain.nix/closure=<hash>`` (weight 50). Helps
       *concurrent* warm-cache: multiple pods in the same submit sharing a
       closure attract toward each other's node. Only matches Pending/Running
       pods, so it does not help across workflow boundaries.

    2. **nodeAffinity** on ``kubernetes.io/hostname In [warm_nodes]`` (weight 90).
       Helps *sequential exact-match* warm-cache: pods from earlier submits
       left a record (the closure label on completed pods, queried at submit
       time by :func:`nix_utils.find_warm_nodes`). nodeAffinity matches
       against node labels directly so it works regardless of pod liveness.

    3. **nodeAffinity** on partial-warm nodes (weight 30). Helps *sequential
       partial-match*: nodes that ran a different closure still share a
       chunk of store paths with this one (glibc, gcc, bash, …). Weaker
       signal than 2, but useful when no exact match exists. Disjoint from
       (2) by construction in ``find_warm_nodes``.

    All are soft — under capacity pressure pods spread to cold nodes rather
    than blocking. Weights compound when a node matches multiple terms.
    Ordering: exact (90) > podAffinity (50) > partial (30), so an
    exact-match node always wins over a partial-only node even when the
    partial node also has a concurrent producer (which would add 50).

    A deep copy keeps the workflow-level affinity dict (shared across all
    roles in a step) from being mutated.
    """
    if closure_hash is None:
        return base_affinity
    affinity = copy.deepcopy(base_affinity) if base_affinity else {}

    # podAffinity: concurrent co-scheduling on closure label.
    pa = affinity.setdefault("podAffinity", {})
    pref = pa.setdefault("preferredDuringSchedulingIgnoredDuringExecution", [])
    pref.append(
        {
            "weight": 50,
            "podAffinityTerm": {
                "labelSelector": {"matchLabels": {"seekr-chain.nix/closure": closure_hash}},
                "topologyKey": "kubernetes.io/hostname",
            },
        }
    )

    # nodeAffinity: sequential exact-match warm-cache via known warm node hostnames.
    if warm_nodes:
        na = affinity.setdefault("nodeAffinity", {})
        na_pref = na.setdefault("preferredDuringSchedulingIgnoredDuringExecution", [])
        na_pref.append(
            {
                "weight": 90,
                "preference": {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": warm_nodes,
                        }
                    ]
                },
            }
        )

    # nodeAffinity: sequential partial-match warm-cache. Lower weight (30)
    # so any exact-match node always outranks a partial-only one. Disjoint
    # from `warm_nodes` by construction in find_warm_nodes — no node
    # appears in both lists.
    if partial_warm_nodes:
        na = affinity.setdefault("nodeAffinity", {})
        na_pref = na.setdefault("preferredDuringSchedulingIgnoredDuringExecution", [])
        na_pref.append(
            {
                "weight": 30,
                "preference": {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": partial_warm_nodes,
                        }
                    ]
                },
            }
        )
    return affinity


def _select_role_runtime(role_config, *, code_path: str | None, interactive: bool):
    """Decide the main container's image, step_args, env, init-container, and volume.

    Returns a tuple ``(main_image, step_args, nix_main_env, nix_init_ctx,
    nix_volume_ctx)``. Three cases:

    1. **Nix-mode consumer** (``role.nix`` set). Main container runs the
       user's script with the closure on PATH. chain-nix-init pulls the
       closure into the volume before main starts. Volume mounts at ``/nix``
       with ``subPath=nix`` so chain-nix-init's ``--store local?root=`` writes
       (which land on disk at ``/nix-shared/nix/store/<hash>``) surface in
       main as ``/nix/store/<hash>`` — the path that the closure's RPATHs
       were baked against.

    2. **Auto-injected builder** (image-mode + ``SEEKR_CHAIN_NIX_CLOSURE``
       in env, produced by ``nix_resolution._make_build_step``). Main runs
       ``nix build`` + ``nix copy --to s3``. Same hostPath volume mounted
       at ``/nix-shared`` (no subPath) so the build script's
       ``--store local?root=/nix-shared`` chroot writes match the consumer's
       on-disk layout. A consumer pod scheduled to the same node by
       closure-hash podAffinity will find the closure already on disk and
       its ``nix copy --from`` becomes a no-op.

    3. **Plain image-mode role**. No nix volume; the standard chain-entrypoint
       wrapper runs the user's script.

    Interactive mode short-circuits step_args to ``sleep <timeout>``.
    """
    nix_main_env: list[dict] = []
    nix_init_ctx: dict | None = None
    nix_volume_ctx: dict | None = None

    if role_config.nix is not None:
        nix_ctx = _resolve_nix_role(role_config, code_path=code_path)
        main_image = nix_ctx["image"]
        nix_main_env = nix_ctx["main_env"]
        nix_init_ctx = {
            "image": resolve_image(nix_ctx["image"]),
            "env": _normalize_env(nix_ctx["init_env"]),
        }
        nix_volume_ctx = {
            "kind": nix_ctx["volume_kind"],
            "hostpath": nix_ctx["hostpath"],
            "mount_path": "/nix",
            "sub_path": "nix",
        }
    else:
        main_image = role_config.image
        # NOTE: hostPath /nix exposes other closures on the node alongside
        # this build's output. That's intentional for cross-pod cache reuse.
        # Inter-pod isolation is not a goal of v1 — revisit only if multi-
        # tenant requirements emerge; the cost is one closure-sized copy
        # at pod startup, fast on local NVMe.
        if (role_config.env or {}).get("SEEKR_CHAIN_NIX_CLOSURE"):
            volume_kind = _user_config.nix_store_volume_kind or _DEFAULT_NIX_STORE_VOLUME_KIND
            nix_volume_ctx = {
                "kind": volume_kind,
                "hostpath": _user_config.nix_store_hostpath or _DEFAULT_NIX_STORE_HOSTPATH,
                "mount_path": "/nix-shared",
                "sub_path": None,
            }

    if interactive:
        timeout = 1 * 60 * 60  # auto-timeout of 1 hour
        logger.warning("Setting auto-timeout of 1 hour")
        step_args = f"sleep {timeout}"
    elif role_config.nix is not None:
        step_args = _NIX_MAIN_STEP_ARGS.format(
            entrypoint=f"{constants.JOB_RESOURCES_PATH}/chain-entrypoint.sh",
        )
    else:
        step_args = f"{constants.JOB_RESOURCES_PATH}/chain-entrypoint.sh"

    return main_image, step_args, nix_main_env, nix_init_ctx, nix_volume_ctx


def _build_role_context(
    role_config,
    workflow_config,
    workflow_secrets: list[dict],
    workflow_name: str,
    js_name: str,
    job_info,
    interactive: bool,
    step_name: str,
    assets_path: Path,
    workflow_affinity: dict | None = None,
) -> dict:
    """Build the Jinja2 template context dict for a single replicated job (role)."""
    js_pod_name = role_config.name
    subdomain = js_name
    master_addr = f"{js_name}-{js_pod_name}-0-0.{subdomain}"

    # S3 path where the init container uploads pod metadata
    remote_md_path = s3_utils.join(
        job_info["remote_step_data_path"],
        f"step={step_name}",
        f"role={role_config.name}",
        "job_index=${NODE_RANK}",
        "pod_index=${JOB_COMPLETION_INDEX}",
        "attempt=${RESTART_ATTEMPT}",
        "md.json",
    )

    # Local path inside the container where role-specific assets are extracted
    role_asset_path = _generate_role_asset_path(
        step_name=step_name, role_name=role_config.name, parent=constants.JOB_ASSET_PATH
    )

    # S3 path for log sidecar output
    remote_step_data_path = s3_utils.join(
        job_info["remote_step_data_path"],
        f"step={step_name}",
        f"role={role_config.name}",
        "job_index=${NODE_RANK}",
        "pod_index=${JOB_COMPLETION_INDEX}",
    )
    s3_bucket, s3_step_data_prefix = s3_utils.parse_s3_uri(remote_step_data_path)
    upload_timeout = int(workflow_config.logging.upload_timeout.total_seconds())

    code_path = workflow_config.code.path if workflow_config.code else None
    main_image, step_args, nix_main_env, nix_init_ctx, nix_volume_ctx = _select_role_runtime(
        role_config,
        code_path=code_path,
        interactive=interactive,
    )

    pvcs_raw, pvc_mounts = _get_pvcs(role_config)
    # Template only needs the volume name; structure is defined in the template
    pvcs = [{"name": v["name"]} for v in pvcs_raw]

    shm_unlimited = role_config.resources.shm_size.upper() in {"UNLIMITED"}

    raw_env = _get_env(
        workflow_config,
        role_config,
        workflow_secrets,
        master_addr,
        workflow_name=workflow_name,
        jobset_name=js_name,
    )

    _construct_hostfile(
        js_name,
        js_pod_name,
        subdomain,
        role_config=role_config,
        assets_path=assets_path,
        step_name=step_name,
    )

    # Nix-mode env additions appended after the standard env so they don't
    # override (and so they're visible to chain-entrypoint via env inheritance).
    if nix_main_env:
        raw_env = raw_env + nix_main_env

    # Closure-hash label + closure-hash podAffinity term. Applies to both
    # nix-mode user steps (which CONSUME the closure) and auto-injected
    # build steps (which PRODUCE it via env-var marker) — same label, so
    # a user step naturally prefers the node where the build step ran.
    closure_hash = _detect_closure_hash(role_config, code_path=code_path)
    warm_nodes = _detect_warm_nodes(role_config)
    partial_warm_nodes = _detect_partial_warm_nodes(role_config)
    role_affinity = _merge_affinity_with_closure(
        workflow_affinity,
        closure_hash,
        warm_nodes,
        partial_warm_nodes,
    )

    return {
        "name": js_pod_name,
        "replicas": role_config.resources.num_nodes,
        "image": resolve_image(main_image),
        "privileged": role_config.resources.security.privileged,
        "resources": _get_step_resources(role_config),
        "env": _normalize_env(raw_env),
        "pvcs": pvcs,
        "pvc_mounts": pvc_mounts,
        "host_network": role_config.resources.host_network,
        "shm_size": role_config.resources.shm_size,
        "shm_unlimited": shm_unlimited,
        "step_args": step_args,
        # Init container image and computed paths
        "init_image": resolve_image(_INIT_IMAGE),
        "remote_md_path": remote_md_path,
        "role_asset_path": str(role_asset_path),
        "init_upload_md_cmd": (
            f'printf \'{{"pod_name":"%s"}}\' $SEEKR_CHAIN_POD_INSTANCE_ID > /tmp/metadata.json'
            f" && s5cmd cp /tmp/metadata.json {remote_md_path}"
        ),
        # Nix integration — populated only for nix-mode roles, None otherwise.
        # The template checks `role.nix_init` / `role.nix_volume` and renders
        # the chain-nix-init init container + nix-store volume conditionally.
        "nix_init": nix_init_ctx,
        "nix_volume": nix_volume_ctx,
        # Closure-hash label + per-role affinity (workflow base + closure term).
        # Template uses `role.affinity` instead of the top-level `affinity` so
        # different roles in the same step can have different closure terms.
        "closure_hash": closure_hash,
        "affinity": role_affinity,
        # Log sidecar
        "log_sidecar_image": resolve_image("fluent/fluent-bit:2.2-debug"),
        "log_sidecar_s3_bucket": s3_bucket,
        "log_sidecar_s3_prefix": s3_step_data_prefix,
        "log_sidecar_upload_timeout": str(upload_timeout),
    }


def _build_jobset_labels(workflow_config) -> dict | None:
    labels = {}
    if workflow_config.scheduling is not None:
        labels["kueue.x-k8s.io/queue-name"] = workflow_config.scheduling.queue
        if workflow_config.scheduling.priority is not None:
            labels["kueue.x-k8s.io/priority-class"] = workflow_config.scheduling.priority
    return labels or None


def _build_affinity(workflow_config) -> tuple[dict | None, list[str]]:
    """Return (affinity_dict, pack_groups).

    affinity_dict is the Kubernetes affinity object for the pod spec, or None.
    pack_groups is the list of group names from ATTRACT pod rules — these become
    seekr-chain/pg.<group> labels on every pod.
    """
    if not workflow_config.affinity:
        return None, []

    node_required_terms = []
    node_preferred_terms = []
    pod_affinity_required = []
    pod_affinity_preferred = []
    pod_anti_required = []
    pod_anti_preferred = []
    pack_groups = []

    for rule in workflow_config.affinity:
        if rule.type == "NODE":
            expressions = []
            if rule.hostnames:
                if rule.direction == "ATTRACT":
                    expressions.append(
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": list(rule.hostnames),
                        }
                    )
                else:
                    for hostname in rule.hostnames:
                        expressions.append(
                            {
                                "key": "kubernetes.io/hostname",
                                "operator": "NotIn",
                                "values": [hostname],
                            }
                        )
            if rule.labels:
                op = "In" if rule.direction == "ATTRACT" else "NotIn"
                for key, values in rule.labels.items():
                    expressions.append({"key": key, "operator": op, "values": values})

            if expressions:
                term = {"matchExpressions": expressions}
                if rule.required:
                    node_required_terms.append(term)
                else:
                    node_preferred_terms.append({"weight": 1, "preference": term})

        elif rule.type == "POD":
            label_key = f"seekr-chain/pg.{rule.group}"
            pod_term = {
                "labelSelector": {"matchLabels": {label_key: "true"}},
                "topologyKey": "kubernetes.io/hostname",
            }
            if rule.direction == "ATTRACT":
                pack_groups.append(rule.group)
                if rule.required:
                    pod_affinity_required.append(pod_term)
                else:
                    pod_affinity_preferred.append({"weight": 100, "podAffinityTerm": pod_term})
            else:
                if rule.required:
                    pod_anti_required.append(pod_term)
                else:
                    pod_anti_preferred.append({"weight": 100, "podAffinityTerm": pod_term})

    affinity = {}

    if node_required_terms or node_preferred_terms:
        node_affinity = {}
        if node_required_terms:
            node_affinity["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": node_required_terms}
        if node_preferred_terms:
            node_affinity["preferredDuringSchedulingIgnoredDuringExecution"] = node_preferred_terms
        affinity["nodeAffinity"] = node_affinity

    if pod_affinity_required or pod_affinity_preferred:
        pod_affinity = {}
        if pod_affinity_required:
            pod_affinity["requiredDuringSchedulingIgnoredDuringExecution"] = pod_affinity_required
        if pod_affinity_preferred:
            pod_affinity["preferredDuringSchedulingIgnoredDuringExecution"] = pod_affinity_preferred
        affinity["podAffinity"] = pod_affinity

    if pod_anti_required or pod_anti_preferred:
        pod_anti = {}
        if pod_anti_required:
            pod_anti["requiredDuringSchedulingIgnoredDuringExecution"] = pod_anti_required
        if pod_anti_preferred:
            pod_anti["preferredDuringSchedulingIgnoredDuringExecution"] = pod_anti_preferred
        affinity["podAntiAffinity"] = pod_anti

    return affinity or None, pack_groups


def _get_step_resources(config) -> dict:
    CPU_RESOURCE_MARGIN = 0.95
    MEM_RESOURCE_MARGIN = 0.95
    ES_RESOURCE_MARGIN = 0.80

    resources = {
        "cpu": config.resources.cpus_per_node,
        "memory": config.resources.mem_per_node,
        "ephemeral-storage": config.resources.ephemeral_storage_per_node,
    }
    if config.resources.gpus_per_node:
        resources[config.resources.gpu_type.value] = config.resources.gpus_per_node

    # If CPU or MEMORY unset, infer from GPUs
    if resources["cpu"] in [None, "AUTO"]:
        if not config.resources.gpu_type:
            raise ValueError("Unable to infer CPU without setting GPUs")
        # This call is cached, so we can do it as needed
        node_resources = k8s_utils.get_node_resources_by_gpu()[config.resources.gpu_type]
        cpus_per_gpu = node_resources["cpu"] / node_resources["gpu"] * CPU_RESOURCE_MARGIN
        resources["cpu"] = f"{int((cpus_per_gpu * config.resources.gpus_per_node) * 1000)}m"
        logger.info(f"Inferring CPUs/GPU: {cpus_per_gpu}/GPU -> {resources['cpu']}")

    if resources["memory"] in [None, "AUTO"]:
        if not config.resources.gpu_type:
            raise ValueError("Unable to infer MEMORY without setting GPUs")
        node_resources = k8s_utils.get_node_resources_by_gpu()[config.resources.gpu_type]
        memory_per_gpu = MEM_RESOURCE_MARGIN * node_resources["memory"] / node_resources["gpu"]
        resources["memory"] = int(memory_per_gpu * config.resources.gpus_per_node)
        logger.info(f"Inferring MEM/GPU: {format_bytes(memory_per_gpu)}/GPU -> {format_bytes(resources['memory'])}")

    if resources["ephemeral-storage"] in [None, "AUTO"]:
        if not config.resources.gpu_type:
            raise ValueError("Unable to infer EPHEMERAL_STORAGE without setting GPUs")
        node_resources = k8s_utils.get_node_resources_by_gpu()[config.resources.gpu_type]
        es_per_gpu = ES_RESOURCE_MARGIN * node_resources["ephemeral-storage"] / node_resources["gpu"]
        resources["ephemeral-storage"] = int(es_per_gpu * config.resources.gpus_per_node)
        logger.info(
            f"Inferring Storage/GPU: {format_bytes(es_per_gpu)}/GPU -> {format_bytes(resources['ephemeral-storage'])}"
        )

    return resources


def _build_success_policy(step_config) -> dict | None:
    sp_config = getattr(step_config, "success_policy", None)
    if sp_config is None:
        return None

    policy = {"operator": sp_config.operator.capitalize()}
    if sp_config.target_roles:
        policy["targetReplicatedJobs"] = sp_config.target_roles
    return policy


def _normalize_literal(lit: str) -> str:
    return "".join([x.capitalize() for x in lit.split("_")])


def _build_failure_policy(step_config) -> dict | None:
    fp_config = getattr(step_config, "failure_policy", None)
    if fp_config is None:
        return None

    policy = {"maxRestarts": fp_config.max_restarts}

    rules = []
    for rule in fp_config.rules:
        rules.append(
            {
                "action": _normalize_literal(rule.action),
                "targetReplicatedJobs": rule.target_roles,
            },
        )
    return policy


def _compute_peermap(role_configs, js_name: str, step_config: StepConfig) -> dict:
    peermap = {}
    for cfg in role_configs:
        js_pod_name = cfg.name
        peermap[js_pod_name] = [f"{js_name}-{js_pod_name}-{i}-0.{js_name}" for i in range(cfg.resources.num_nodes)]

    if isinstance(step_config, SingleRoleStepConfig):
        peermap = peermap[""]

    return peermap


def _write_peermaps_and_scripts(role_configs, js_name, step_config, assets_path):
    peermap = _compute_peermap(role_configs=role_configs, js_name=js_name, step_config=step_config)

    for role_config in role_configs:
        role_path = Path(
            _generate_role_asset_path(step_name=step_config.name, role_name=role_config.name, parent=assets_path)
        )
        role_path.mkdir(exist_ok=True, parents=True)

        script_path = role_path / "script.sh"
        with open(script_path, "w") as f:
            if role_config.shell:
                f.write(f"#!{role_config.shell}\n")
            f.write(textwrap.dedent(role_config.script))
        script_path.chmod(0o755)

        before_script_path = role_path / "before_script.sh"
        with open(before_script_path, "w") as f:
            if role_config.shell:
                f.write(f"#!{role_config.shell}\n")
            f.write(textwrap.dedent(role_config.before_script or ""))
        before_script_path.chmod(0o755)

        after_script_path = role_path / "after_script.sh"
        with open(after_script_path, "w") as f:
            if role_config.shell:
                f.write(f"#!{role_config.shell}\n")
            f.write(textwrap.dedent(role_config.after_script or ""))
        after_script_path.chmod(0o755)

        peermap_path = role_path / "peermap.json"
        with open(peermap_path, "w") as f:
            json.dump(peermap, f, sort_keys=True)


def build_jobset_context(
    workflow_config,
    step_index,
    job_info,
    workflow_name,
    workflow_secrets,
    interactive: bool,
    assets_path: Path,
) -> tuple[str, dict]:
    """Build the Jinja2 template context for a JobSet manifest.

    Returns (js_name, context_dict). The context dict is passed directly to
    the jobset.yaml.j2 template.
    """
    step_config = workflow_config.steps[step_index]
    step_name = step_config.name
    js_name = f"{workflow_name}-{step_name}-js"

    # NORMALIZE SINGLE AND MULTI-ROLE STEPS.
    if isinstance(step_config, SingleRoleStepConfig):
        role_configs = [step_config.model_copy()]
        role_configs[0].name = ""
    else:
        role_configs = step_config.roles

    # Check if we will be over 63 character limit
    for role_config in role_configs:
        if len(f"{js_name}-{role_config.name}-00-00-abcde") > 63:
            js_name = f"{workflow_name.split('-')[-1]}-s{step_index:02d}-js"
            logger.warning(f"Generated jobset name is too long! Shortening to {js_name}")
            break

    _write_peermaps_and_scripts(
        role_configs=role_configs, js_name=js_name, step_config=step_config, assets_path=assets_path
    )

    affinity, pack_groups = _build_affinity(workflow_config)

    context = {
        "js_name": js_name,
        "job_id": job_info["id"],
        "step_name": step_name,
        "workflow_name": workflow_name,
        "remote_assets_path": job_info["remote_assets_path"],
        "success_policy": _build_success_policy(step_config),
        "failure_policy": _build_failure_policy(step_config),
        "affinity": affinity,
        "pack_groups": pack_groups,
        "labels": _build_jobset_labels(workflow_config),
        "roles": [
            _build_role_context(
                role_config=role_config,
                workflow_config=workflow_config,
                workflow_secrets=workflow_secrets,
                workflow_name=workflow_name,
                js_name=js_name,
                job_info=job_info,
                interactive=interactive,
                step_name=step_name,
                assets_path=assets_path,
                workflow_affinity=affinity,
            )
            for role_config in role_configs
        ],
    }

    return js_name, context


def create_jobset_manifest(
    workflow_config,
    step_index,
    job_info,
    workflow_name,
    workflow_secrets,
    interactive: bool,
    assets_path: Path,
) -> tuple[str, str]:
    """Build and render the JobSet manifest.

    Returns (js_name, rendered_yaml_string).
    """
    from seekr_chain.backends.k8s import render

    js_name, context = build_jobset_context(
        workflow_config=workflow_config,
        step_index=step_index,
        job_info=job_info,
        workflow_name=workflow_name,
        workflow_secrets=workflow_secrets,
        interactive=interactive,
        assets_path=assets_path,
    )

    rendered = render.render("jobset.yaml.j2", context)

    return js_name, rendered
