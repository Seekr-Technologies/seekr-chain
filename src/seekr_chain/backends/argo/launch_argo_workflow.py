#!/usr/bin/env python3

import datetime
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import boto3
import kubernetes
import yaml
from botocore.client import BaseClient

from seekr_chain import WorkflowConfig, k8s_utils, s3_utils, utils
from seekr_chain.backends.argo import render
from seekr_chain.backends.argo.argo_workflow import ArgoWorkflow
from seekr_chain.backends.argo.job_info import JobInfo, _resolve_datastore_root, get_job_info
from seekr_chain.backends.argo.jobset import create_jobset_manifest
from seekr_chain.backends.argo.parse_logs import DATA_SCHEMA_VERSION
from seekr_chain.config import StepConfig
from seekr_chain.symlink import symlink
from seekr_chain.tar_directory import tar_directory

logger = logging.getLogger(__name__)


def _create_secrets(workflow_name: str, s3_creds: dict, config: WorkflowConfig):
    secrets = {}
    if config.secrets:
        secrets = config.secrets

    if s3_creds:
        secrets = {**secrets, **{key.upper(): value for key, value in s3_creds.items()}}

    v1 = k8s_utils.get_core_v1_api()

    if secrets:
        # Create the Secret object
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

        # Create the secret in the cluster
        v1.create_namespaced_secret(namespace=config.namespace, body=secret)

        secret_names = "\n".join([f"  {key}" for key in sorted(secrets.keys())])
        logger.info(f"Uploaded workflow secrets:\n{secret_names}")

    # Cleanup old secrets
    max_age_days = 7

    # Only touch secrets we manage
    selector = "app=seekr-chain,managed-by=seekr-chain,type=workflow-secret"
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=max_age_days)

    logger.debug("Cleaning up old secrets")
    resp = v1.list_namespaced_secret(namespace=config.namespace, label_selector=selector)
    for sec in resp.items:
        created = sec.metadata.creation_timestamp  # timezone-aware datetime
        if created and created < cutoff:
            try:
                v1.delete_namespaced_secret(name=sec.metadata.name, namespace=config.namespace)
            except kubernetes.client.exceptions.ApiException as e:
                logger.debug(f"Failed to delete {sec.metadata.name}: {e}")


def _create_step_manifest(
    workflow_config: WorkflowConfig,
    step_index: int,
    job_info: JobInfo,
    workflow_name: str,
    workflow_secrets: list[dict],
    interactive: bool,
    assets_path: Path,
):
    step_config = workflow_config.steps[step_index]

    js_name, js_yaml = create_jobset_manifest(
        workflow_config=workflow_config,
        step_index=step_index,
        job_info=job_info,
        workflow_name=workflow_name,
        workflow_secrets=workflow_secrets,
        interactive=interactive,
        assets_path=assets_path,
    )

    return {
        "name": step_config.name,
        "jobset_name": js_name,
        "jobset_yaml": js_yaml,
    }


def _create_dag_task(step_config: StepConfig) -> dict:
    return {
        "name": step_config.name,
        "dependencies": step_config.depends_on or [],
    }


def _create_workflow_secrets(config: WorkflowConfig, workflow_name: str, s3_creds: dict) -> list[dict]:
    out = []

    secrets = {}
    if config.secrets:
        secrets = config.secrets

    if s3_creds:
        secrets = {**secrets, **{key.upper(): value for key, value in s3_creds.items()}}

    for secret_key in secrets.keys():
        out.append(
            {
                "name": secret_key,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": workflow_name,
                        "key": secret_key,
                    }
                },
            }
        )

    return out


def _create_workflow_manifest(
    config: WorkflowConfig, s3_creds, job_info: JobInfo, interactive: bool, assets_path: Path
) -> tuple[dict, str]:
    """
    Create the overall workflow manifest from the WorkflowConfig.
    """

    workflow_name = f"{job_info['id']}"
    datastore_root = _resolve_datastore_root()

    workflow_secrets = _create_workflow_secrets(config, workflow_name, s3_creds)

    if interactive:
        if len(config.steps) != 1:
            raise ValueError("Interactive jobs may only have a single step")

    # Create step manifests. These are the full definitions of each step.
    steps = [
        _create_step_manifest(
            workflow_config=config,
            step_index=i,
            job_info=job_info,
            workflow_name=workflow_name,
            workflow_secrets=workflow_secrets,
            interactive=interactive,
            assets_path=assets_path,
        )
        for i in range(len(config.steps))
    ]

    # Create dag tasks. This is basically just the step name and its dependencies
    context = {
        "workflow_name": workflow_name,
        "job_id": job_info["id"],
        "job_name": config.name[:63],
        "user": os.environ.get("USER", "unknown")[:63],
        "datastore_root": datastore_root,
        "ttl_seconds": int(config.ttl.total_seconds()),
        "dag_tasks": [_create_dag_task(step_config) for step_config in config.steps],
        "steps": steps,
    }

    rendered = render.render("workflow.yaml.j2", context)
    logger.debug(f"Workflow manifest:\n\n{rendered}\n")

    manifest = yaml.safe_load(rendered)
    return manifest, workflow_name


def _argo_submit(job_info: JobInfo, manifest: dict, config: WorkflowConfig) -> ArgoWorkflow:
    """
    Submit an argo workflow, returning an ArgoWorkflow object
    """

    k8s_custom = k8s_utils.get_custom_objects_api()
    k8s_custom.create_namespaced_custom_object(
        group="argoproj.io",
        version="v1alpha1",
        plural="workflows",
        namespace=config.namespace,
        body=manifest,
    )

    job = ArgoWorkflow(
        id=job_info["id"],
        namespace=config.namespace,
    )

    logger.info(f"Launched argo workflow: {job.name}")

    return job


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
    config: WorkflowConfig, args: dict | None, s3_client: BaseClient, job_info: JobInfo, staging_dir: Path
):
    """
    Package up assets, and upload to s3
    """
    dest = job_info["remote_assets_path"]
    # TODO: Do a better job of using `constants` here

    # CODE
    if config.code is not None:
        logger.info(f"Including code from path: {config.code.path}")
        local_code_dest = staging_dir / "workspace"
        # Just create a symlink, and let the tar operation follow the links
        symlink(Path(config.code.path), local_code_dest, exclude=config.code.exclude, include=config.code.include)
        logger.info(utils.summarize_dir(local_code_dest, detail=False))

    # COPY RESOURCES
    resources_source = Path(__file__).parent / "resources"
    shutil.copytree(resources_source, staging_dir / "resources")

    # ARGS
    local_arg_path = staging_dir / "assets/workflow_args.json"
    if args is None:
        args = {}
    with open(local_arg_path, "w") as f:
        json.dump(args, f)

    with tempfile.NamedTemporaryFile() as tarpath:
        tarpath = Path(tarpath.name)
        logger.info(f"Packaging assets from staging dir: {staging_dir}")
        tar_directory(staging_dir, tarpath)
        logger.info(f"Uploading assets to {dest} ({utils.format_bytes(tarpath.stat().st_size)})")
        s3_utils.upload_file(tarpath, dest, s3_client)


def _generate_job_info(s3_client: BaseClient, datastore_root: str = None) -> JobInfo:
    # Generate guaranteed unique ID, using datastore.
    # It is _possible_ that two jobs generate the same ID at the same time, but _extremely_ unlikely

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


def launch_argo_workflow(
    config: dict | WorkflowConfig, interactive: bool = False, attach: bool = True, args: dict | None = None
) -> ArgoWorkflow:
    """
    Launch an argo workflow. Returns an ArgoWorkflow object.

    Parameters
    ----------
    config
    interactive
    attach : If True, and Interactive, automatically attach to workflow
    args : JSON-serializable arg dict, which will be available in the job as an environment variable
    """
    if isinstance(config, dict):
        config = WorkflowConfig.model_validate(config)

    s3_client, s3_creds = _get_s3_client_and_creds()

    job_info = _generate_job_info(s3_client)

    with tempfile.TemporaryDirectory() as staging_dir:
        staging_dir = Path(staging_dir)
        assets_path = staging_dir / "assets"

        manifest, workflow_name = _create_workflow_manifest(config, s3_creds, job_info, interactive, assets_path)

        _package_assets(config, args, s3_client, job_info, staging_dir)

    _create_secrets(workflow_name, s3_creds, config)

    workflow = _argo_submit(job_info, manifest, config)

    if interactive and attach:
        workflow.attach()

    return workflow
