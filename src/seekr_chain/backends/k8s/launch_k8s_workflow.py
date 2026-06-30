#!/usr/bin/env python3

import datetime
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import boto3
import dotenv
import kubernetes
from botocore.client import BaseClient

from seekr_chain import WorkflowConfig, constants, k8s_utils, s3_utils, utils
from seekr_chain.backends.k8s.job_info import JobInfo, _resolve_datastore_root, get_job_info
from seekr_chain.backends.k8s.jobset import _INIT_IMAGE, create_jobset_manifest
from seekr_chain.backends.k8s.parse_logs import DATA_SCHEMA_VERSION
from seekr_chain.backends.k8s.rbac import detect_service_account
from seekr_chain.config import EnvSource, SecretRefSource
from seekr_chain.nix_resolution import resolve_nix_steps
from seekr_chain.symlink import symlink
from seekr_chain.tar_directory import tar_directory
from seekr_chain.user_config import config as _user_config

logger = logging.getLogger(__name__)

_DEFAULT_CONTROLLER_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-controller:1.0.0@sha256:7a8700bebddfaecef8174e98ef3b408295fee29e005adb28192771ac901ee6d3"
_CONTROLLER_IMAGE = _user_config.controller_image or _DEFAULT_CONTROLLER_IMAGE


def _resolve_env_secrets(config: WorkflowConfig) -> dict[str, str]:
    """Resolve EnvSource secret values against the local environment and any .env file."""
    env_entries = {k: v for k, v in (config.secrets or {}).items() if isinstance(v, EnvSource)}
    if not env_entries:
        return {}

    dotenv_path = dotenv.find_dotenv(usecwd=True)
    dotenv_values = dotenv.dotenv_values(dotenv_path) if dotenv_path else {}
    merged = {**dotenv_values, **os.environ}

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key, source in env_entries.items():
        var_name = source.env if isinstance(source.env, str) else key
        value = merged.get(var_name)
        if value is None:
            missing.append(var_name)
        else:
            resolved[key] = value

    if missing:
        raise RuntimeError(
            f"The following environment variable(s) required by secrets are not set: "
            f"{', '.join(missing)}\n\n"
            "Set them in your shell or add them to a .env file in your project directory."
        )
    return resolved


def _create_secrets(workflow_name: str, s3_creds: dict, config: WorkflowConfig):
    # Collect inline string values and resolved EnvSource values; skip SecretRefSource.
    secrets: dict[str, str] = {}
    for key, value in (config.secrets or {}).items():
        if isinstance(value, str):
            secrets[key] = value
        # SecretRefSource entries are referenced directly in pods; values are never copied.

    secrets.update(_resolve_env_secrets(config))

    if s3_creds:
        # Only fill in creds the user hasn't already set — explicit config always wins.
        for k, v in s3_creds.items():
            if k.upper() not in secrets:
                secrets[k.upper()] = v

    v1 = k8s_utils.get_core_v1_api()

    if secrets:
        secret = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name=workflow_name,
                labels={
                    "app": "seekr-chain",
                    "managed-by": "seekr-chain",
                    "type": "workflow-secret",
                },
            ),
            type="Opaque",
            string_data=secrets,
        )
        v1.create_namespaced_secret(namespace=config.namespace, body=secret)
        logger.info("Uploaded workflow secrets (count=%d)", len(secrets))

    # Cleanup old secrets
    max_age_days = 7
    selector = "app=seekr-chain,managed-by=seekr-chain,type=workflow-secret"
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=max_age_days)

    logger.debug("Cleaning up old secrets")
    try:
        resp = v1.list_namespaced_secret(namespace=config.namespace, label_selector=selector)
    except kubernetes.client.exceptions.ApiException as e:
        logger.warning(
            "Skipping stale-secret cleanup: unable to list secrets in namespace %r "
            "(typically an RBAC permission issue). status=%s reason=%s",
            config.namespace,
            e.status,
            e.reason,
        )
        return

    for sec in resp.items:
        created = sec.metadata.creation_timestamp
        if created and created < cutoff:
            try:
                v1.delete_namespaced_secret(name=sec.metadata.name, namespace=config.namespace)
            except kubernetes.client.exceptions.ApiException as e:
                logger.debug(f"Failed to delete {sec.metadata.name}: {e}")


def _create_workflow_secrets(config: WorkflowConfig, workflow_name: str, s3_creds: dict) -> list[dict]:
    """Build the list of secretKeyRef env-var stanzas for pods."""
    out = []

    for key, value in (config.secrets or {}).items():
        if isinstance(value, SecretRefSource):
            # Reference the existing secret directly — value is never copied.
            ref_key = value.secretRef.key or key
            out.append({"name": key, "valueFrom": {"secretKeyRef": {"name": value.secretRef.name, "key": ref_key}}})
        else:
            # Inline strings and EnvSource values are stored in the per-workflow K8s Secret.
            out.append({"name": key, "valueFrom": {"secretKeyRef": {"name": workflow_name, "key": key}}})

    # S3 credentials are stored in the per-workflow K8s Secret.
    # Skip any key the user has already defined — explicit config always wins.
    existing_keys = {entry["name"] for entry in out}
    for cred_key in (s3_creds or {}).keys():
        env_key = cred_key.upper()
        if env_key in existing_keys:
            logger.warning(
                "Skipping automatic injection of an S3 credential: "
                "a secret with that name is already defined in your workflow config."
            )
            continue
        out.append({"name": env_key, "valueFrom": {"secretKeyRef": {"name": workflow_name, "key": env_key}}})

    return out


def _get_s3_client_and_creds() -> tuple[BaseClient, dict]:
    from botocore.exceptions import NoCredentialsError, PartialCredentialsError

    try:
        client = boto3.client("s3")
        creds = client._get_credentials()
        if creds is None:
            raise NoCredentialsError()
        creds_dict = {"aws_access_key_id": creds.access_key, "aws_secret_access_key": creds.secret_key}
    except (NoCredentialsError, PartialCredentialsError) as e:
        raise RuntimeError(
            f"AWS credentials not found: {e}\n\n"
            "Ensure valid AWS credentials are available:\n"
            "  - Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables, or\n"
            "  - Configure credentials via 'aws configure', or\n"
            "  - Use an IAM instance profile"
        ) from e

    return client, creds_dict


def _package_assets(
    config: WorkflowConfig,
    args: dict | None,
    s3_client: BaseClient,
    job_info: JobInfo,
    staging_dir: Path,
    workflow_name: str,
    workflow_secrets: list[dict],
):
    """Package up assets (code, scripts, jobset manifests, DAG definition) and upload to S3."""
    dest = job_info["remote_assets_path"]

    # CODE
    if config.code is not None:
        logger.info(f"Including code from path: {config.code.path}")
        local_code_dest = staging_dir / "workspace"
        symlink(Path(config.code.path), local_code_dest, exclude=config.code.exclude, include=config.code.include)
        logger.info(utils.summarize_dir(local_code_dest, detail=False))

    # COPY RESOURCES (includes chain-entrypoint.sh, fluentbit, and controller.py)
    resources_source = Path(__file__).parent / "resources"
    shutil.copytree(resources_source, staging_dir / "resources")

    # ARGS
    assets_path = staging_dir / "assets"
    local_arg_path = assets_path / "workflow_args.json"
    if args is None:
        args = {}
    with open(local_arg_path, "w") as f:
        json.dump(args, f)

    # Write per-step assets (scripts, peermaps, hostfiles, jobset manifests)
    dag_entries = []
    for i, step_config in enumerate(config.steps):
        js_name, js_yaml = create_jobset_manifest(
            workflow_config=config,
            step_index=i,
            job_info=job_info,
            workflow_name=workflow_name,
            workflow_secrets=workflow_secrets,
            interactive=False,
            assets_path=assets_path,
        )

        # Write jobset manifest alongside the step's other assets
        step_asset_dir = assets_path / f"step={step_config.name}"
        step_asset_dir.mkdir(exist_ok=True, parents=True)
        jobset_manifest_path = step_asset_dir / "jobset.yaml"
        with open(jobset_manifest_path, "w") as f:
            f.write(js_yaml)

        dag_entries.append(
            {
                "name": step_config.name,
                "depends_on": step_config.depends_on or [],
            }
        )

    # Write DAG definition for controller.py
    dag_path = assets_path / "dag.json"
    with open(dag_path, "w") as f:
        json.dump(dag_entries, f)

    with tempfile.NamedTemporaryFile() as tarpath:
        tarpath = Path(tarpath.name)
        logger.info(f"Packaging assets from staging dir: {staging_dir}")
        tar_directory(staging_dir, tarpath)
        logger.info(f"Uploading assets to {dest} ({utils.format_bytes(tarpath.stat().st_size)})")
        s3_utils.upload_file(tarpath, dest, s3_client)


def _generate_job_info(s3_client: BaseClient, datastore_root: str = None) -> JobInfo:
    n = 6
    job_info = None
    while workflow_id := utils.generate_id(n):
        job_info = get_job_info(workflow_id, datastore_root=datastore_root)
        if not s3_utils.is_dir(job_info["s3_path"], s3_client):
            break
        else:
            n += 1

    if job_info is None:
        raise ValueError("Unable to generate job id!")
    s3_utils.touch(job_info["remote_sentinel"], s3_client)
    with tempfile.NamedTemporaryFile() as tmpfile:
        with open(tmpfile.name, "w") as f:
            f.write(DATA_SCHEMA_VERSION)
        s3_utils.upload_file(tmpfile.name, job_info["remote_version_path"], s3_client)
    return job_info


def _build_controller_job(
    workflow_id: str,
    config: WorkflowConfig,
    job_info: JobInfo,
    workflow_secrets: list[dict],
    datastore_root: str,
    interactive: bool,
    service_account: str,
) -> dict:
    """Build the batch/v1 Job manifest for the controller pod."""
    controller_image = _CONTROLLER_IMAGE
    controller_command = ["python", f"{constants.JOB_RESOURCES_PATH}/controller.py"]

    # Env vars for the controller's init container (S3 download via s5cmd)
    init_env = [
        {
            "name": "AWS_ACCESS_KEY_ID",
            "valueFrom": {"secretKeyRef": {"name": workflow_id, "key": "AWS_ACCESS_KEY_ID"}},
        },
        {
            "name": "AWS_SECRET_ACCESS_KEY",
            "valueFrom": {"secretKeyRef": {"name": workflow_id, "key": "AWS_SECRET_ACCESS_KEY"}},
        },
        {
            "name": "S3_ENDPOINT_URL",
            "valueFrom": {"secretKeyRef": {"name": workflow_id, "key": "S3_ENDPOINT_URL", "optional": True}},
        },
        {
            "name": "AWS_REGION",
            "valueFrom": {"secretKeyRef": {"name": workflow_id, "key": "AWS_REGION", "optional": True}},
        },
    ]

    # Env vars for the controller's main container
    controller_env = [
        {"name": "SEEKR_CHAIN_NAMESPACE", "value": config.namespace},
        {"name": "SEEKR_CHAIN_JOB_ASSET_PATH", "value": constants.JOB_ASSET_PATH},
        {
            "name": "SEEKR_CHAIN_CONTROLLER_JOB_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}},
        },
        {
            # batch.kubernetes.io/controller-uid is automatically stamped on every pod
            # created by a Job — gives us the Job's UID without any API call or RBAC.
            "name": "SEEKR_CHAIN_CONTROLLER_JOB_UID",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/controller-uid']"}},
        },
    ] + workflow_secrets

    # Add SEEKRCHAIN_DATASTORE_ROOT so the controller can call get_job_info if needed
    if datastore_root:
        controller_env.append({"name": "SEEKRCHAIN_DATASTORE_ROOT", "value": datastore_root})

    init_containers = [
        {
            "name": "chain-init",
            "image": _INIT_IMAGE,
            "workingDir": "/seekr-chain",
            "command": ["sh", "-c"],
            "args": [
                f"set -e"
                f" && s5cmd cp {job_info['remote_assets_path']} /seekr-chain/assets.tar.gz"
                f" && tar -xzf /seekr-chain/assets.tar.gz -C /seekr-chain"
                f" && rm /seekr-chain/assets.tar.gz"
            ],
            "volumeMounts": [{"name": "workspace", "mountPath": "/seekr-chain"}],
            "env": init_env,
        },
    ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": workflow_id,
            "namespace": config.namespace,
            "labels": {
                "seekr-chain/job-id": workflow_id,
                "seekr-chain/job-name": config.name[:63],
                "seekr-chain/user": os.environ.get("USER", "unknown")[:63],
            },
            "annotations": {
                "seekr-chain/datastore-root": datastore_root or "",
                "seekr-chain/step-count": str(len(config.steps)),
            },
        },
        "spec": {
            "backoffLimit": 10,
            "ttlSecondsAfterFinished": int(config.ttl.total_seconds()),
            "template": {
                "metadata": {
                    "labels": {
                        "seekr-chain/job-id": workflow_id,
                        "seekr-chain/is-controller": "true",
                    }
                },
                "spec": {
                    "serviceAccountName": service_account,
                    "restartPolicy": "Never",
                    "initContainers": init_containers,
                    "volumes": [{"name": "workspace", "emptyDir": {}}],
                    "containers": [
                        {
                            "name": "controller",
                            "image": controller_image,
                            "command": controller_command,
                            "env": controller_env,
                            "volumeMounts": [{"name": "workspace", "mountPath": "/seekr-chain"}],
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "livenessProbe": {
                                "exec": {
                                    "command": [
                                        "sh",
                                        "-c",
                                        "[ $(( $(date +%s) - $(date +%s -r /tmp/controller-heartbeat) )) -lt 300 ]",
                                    ]
                                },
                                "initialDelaySeconds": 30,
                                "periodSeconds": 60,
                                "failureThreshold": 5,
                            },
                        }
                    ],
                },
            },
        },
    }


def launch_k8s_workflow(
    config: dict | WorkflowConfig, interactive: bool = False, attach: bool = True, args: dict | None = None
):
    """Launch a k8s controller workflow. Returns a K8sWorkflow object."""
    from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

    if isinstance(config, dict):
        config = WorkflowConfig.model_validate(config)

    # Walk nix-mode roles, evaluate expressions, check the store, and inject
    # in-cluster build steps for any missing closures. No-op when there are
    # no nix-mode roles, so this is safe to call unconditionally.
    config = resolve_nix_steps(config)

    if interactive:
        if len(config.steps) != 1:
            raise ValueError("Interactive jobs may only have a single step")

    s3_client, s3_creds = _get_s3_client_and_creds()

    datastore_root = _resolve_datastore_root()
    job_info = _generate_job_info(s3_client, datastore_root=datastore_root)
    workflow_id = job_info["id"]

    workflow_secrets = _create_workflow_secrets(config, workflow_id, s3_creds)

    kubernetes.config.load_kube_config(config_file=os.environ.get("KUBECONFIG"))

    service_account = detect_service_account(config.namespace)

    with tempfile.TemporaryDirectory() as staging_dir:
        staging_dir = Path(staging_dir)
        # Create assets dir upfront so _package_assets can write dag.json there
        (staging_dir / "assets").mkdir(parents=True, exist_ok=True)

        _package_assets(
            config=config,
            args=args,
            s3_client=s3_client,
            job_info=job_info,
            staging_dir=staging_dir,
            workflow_name=workflow_id,
            workflow_secrets=workflow_secrets,
        )

    _create_secrets(workflow_id, s3_creds, config)

    job_manifest = _build_controller_job(
        workflow_id=workflow_id,
        config=config,
        job_info=job_info,
        workflow_secrets=workflow_secrets,
        datastore_root=datastore_root,
        interactive=interactive,
        service_account=service_account,
    )

    k8s_batch = kubernetes.client.BatchV1Api()
    k8s_batch.create_namespaced_job(namespace=config.namespace, body=job_manifest)
    logger.info(f"Launched controller job: {workflow_id}")

    workflow = K8sWorkflow(id=workflow_id, namespace=config.namespace)

    if interactive and attach:
        workflow.attach()

    return workflow
