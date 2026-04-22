#!/usr/bin/env python3

import json
import logging
import stat
import textwrap
from pathlib import Path
from typing import Optional

from seekr_chain import constants, k8s_utils, s3_utils
from seekr_chain.config import (
    SingleRoleStepConfig,
    StepConfig,
    WorkflowConfig,
)
from seekr_chain.utils import format_bytes, resolve_image

logger = logging.getLogger(__name__)


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

    # Main container command
    if interactive:
        timeout = 1 * 60 * 60  # auto-timeout of 1 hour
        logger.warning("Setting auto-timeout of 1 hour")
        step_args = f"sleep {timeout}"
    else:
        step_args = f"{constants.JOB_RESOURCES_PATH}/chain-entrypoint.sh"

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

    return {
        "name": js_pod_name,
        "replicas": role_config.resources.num_nodes,
        "image": resolve_image(role_config.image),
        "privileged": role_config.resources.security.privileged,
        "resources": _get_step_resources(role_config),
        "env": _normalize_env(raw_env),
        "pvcs": pvcs,
        "pvc_mounts": pvc_mounts,
        "host_network": role_config.resources.host_network,
        "shm_size": role_config.resources.shm_size,
        "shm_unlimited": shm_unlimited,
        "step_args": step_args,
        # Init container images and computed paths
        "init_aws_cli_image": resolve_image("amazon/aws-cli:2.25.11"),
        "init_alpine_image": resolve_image("alpine:3.22.0"),
        "init_busybox_image": resolve_image("busybox:1.37-uclibc"),
        "remote_md_path": remote_md_path,
        "role_asset_path": str(role_asset_path),
        "init_upload_md_cmd": (
            f'printf \'{{"pod_name":"%s"}}\' $SEEKR_CHAIN_POD_INSTANCE_ID | aws s3 cp - {remote_md_path}'
        ),
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
    CPU_RESOURCE_MARGIN = 0.99
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
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)

        before_script_path = role_path / "before_script.sh"
        with open(before_script_path, "w") as f:
            if role_config.shell:
                f.write(f"#!{role_config.shell}\n")
            f.write(textwrap.dedent(role_config.before_script or ""))
        before_script_path.chmod(before_script_path.stat().st_mode | stat.S_IXUSR)

        after_script_path = role_path / "after_script.sh"
        with open(after_script_path, "w") as f:
            if role_config.shell:
                f.write(f"#!{role_config.shell}\n")
            f.write(textwrap.dedent(role_config.after_script or ""))
        after_script_path.chmod(after_script_path.stat().st_mode | stat.S_IXUSR)

        peermap_path = role_path / "peermap.json"
        with open(peermap_path, "w") as f:
            json.dump(peermap, f, sort_keys=True)


def build_jobset_context(
    workflow_config, step_index, job_info, workflow_name, workflow_secrets, interactive: bool, assets_path: Path
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
            )
            for role_config in role_configs
        ],
    }

    return js_name, context


def create_jobset_manifest(
    workflow_config, step_index, job_info, workflow_name, workflow_secrets, interactive: bool, assets_path: Path
) -> tuple[str, str]:
    """Build and render the JobSet manifest.

    Returns (js_name, rendered_yaml_string).
    """
    from seekr_chain.backends.argo import render

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
