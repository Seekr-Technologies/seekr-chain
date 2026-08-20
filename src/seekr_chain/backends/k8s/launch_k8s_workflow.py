#!/usr/bin/env python3

import concurrent.futures
import datetime
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import dotenv
import kubernetes

from seekr_chain import WorkflowConfig, constants, remote_fs, utils
from seekr_chain.backends.k8s import ttl
from seekr_chain.backends.k8s.job_info import JobInfo, _resolve_datastore_root, get_job_info
from seekr_chain.backends.k8s.jobset import _INIT_IMAGE, create_jobset_manifest
from seekr_chain.backends.k8s.parse_logs import DATA_SCHEMA_VERSION
from seekr_chain.backends.k8s.rbac import detect_service_account
from seekr_chain.config import EnvSource, SecretRefSource
from seekr_chain.k8s_api import kube
from seekr_chain.nix_resolution import process_nix
from seekr_chain.symlink import symlink
from seekr_chain.tar_directory import tar_directory
from seekr_chain.user_config import config as _user_config

logger = logging.getLogger(__name__)

_DEFAULT_CONTROLLER_IMAGE = "ghcr.io/seekr-technologies/seekr-chain-controller:1.1.0@sha256:de8163cc3652deea9a194fd09bf0d2c167ff1cbe4b51bb424c419adda7b8e97b"
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

    v1 = kube.core_v1

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


def _get_s3_creds() -> dict:
    from botocore.exceptions import NoCredentialsError, PartialCredentialsError

    try:
        creds = remote_fs._get_s3_client()._get_credentials()
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

    return creds_dict


def _package_assets(
    config: WorkflowConfig,
    args: dict | None,
    job_info: JobInfo,
    staging_dir: Path,
    workflow_name: str,
    workflow_secrets: list[dict],
    interactive: bool,
):
    """Package up assets (code, scripts, jobset manifests, DAG definition) and upload to S3.

    Code is already staged into ``staging_dir / "workspace"`` by the caller
    (see ``launch_k8s_workflow``) before this runs — this just logs it.
    """
    dest = job_info["remote_assets_path"]

    if config.code is not None:
        logger.info(utils.summarize_dir(staging_dir / "workspace", detail=False))

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
            interactive=interactive,
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
        remote_fs.upload(tarpath, dest)


def _generate_job_info(datastore_root: str = None) -> JobInfo:
    n = 6
    job_info = None
    while workflow_id := utils.generate_id(n):
        job_info = get_job_info(workflow_id, datastore_root=datastore_root)
        if not remote_fs.exists(job_info["s3_path"]):
            break
        else:
            n += 1

    if job_info is None:
        raise ValueError("Unable to generate job id!")
    with tempfile.NamedTemporaryFile() as sentinel_file:
        remote_fs.upload(sentinel_file.name, job_info["remote_sentinel"])
    with tempfile.NamedTemporaryFile() as tmpfile:
        with open(tmpfile.name, "w") as f:
            f.write(DATA_SCHEMA_VERSION)
        remote_fs.upload(tmpfile.name, job_info["remote_version_path"])
    return job_info


def _build_controller_jobset(
    workflow_id: str,
    config: WorkflowConfig,
    job_info: JobInfo,
    workflow_secrets: list[dict],
    datastore_root: str,
    interactive: bool,
    service_account: str,
) -> dict:
    """Build the jobset.x-k8s.io/v1alpha2 JobSet manifest for the controller pod.

    Runs the controller as a JobSet (rather than a plain batch/v1 Job) so its
    ``status.terminalState`` is derivable the same way as worker JobSets, using
    only the jobset.x-k8s.io RBAC workers already need — no ``batch/jobs``
    permissions, which neither the dedicated SA nor the Argo Workflows
    fallback SA grants.
    """
    # Per-workflow config.controller_image overrides user config and default.
    controller_image = config.controller_image or _CONTROLLER_IMAGE
    controller_command = ["python", "-m", "controller"]

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
        {"name": "SEEKR_CHAIN_CONTROLLER_JOB_NAME", "value": workflow_id},
        {"name": "PYTHONPATH", "value": constants.JOB_RESOURCES_PATH},
    ] + workflow_secrets

    # Add SEEKRCHAIN_DATASTORE_ROOT so the controller can call get_job_info if needed
    if datastore_root:
        controller_env.append({"name": "SEEKRCHAIN_DATASTORE_ROOT", "value": datastore_root})

    # The controller ships status.json to S3 itself via s5cmd, so it needs the
    # same S3 credentials as the init container plus the destination path.
    controller_env += init_env
    controller_env.append({"name": "SEEKR_CHAIN_REMOTE_STATUS_PATH", "value": job_info["remote_status_path"]})

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
        "apiVersion": "jobset.x-k8s.io/v1alpha2",
        "kind": "JobSet",
        "metadata": {
            "name": workflow_id,
            "namespace": config.namespace,
            "labels": {
                "seekr-chain/job-id": workflow_id,
                "seekr-chain/job-name": config.name[:63],
                "seekr-chain/user": os.environ.get("USER", "unknown")[:63],
                "seekr-chain/is-controller": "true",
            },
            "annotations": {
                "seekr-chain/datastore-root": datastore_root or "",
                "seekr-chain/step-count": str(len(config.steps)),
            },
        },
        "spec": {
            "replicatedJobs": [
                {
                    "name": "controller",
                    "replicas": 1,
                    "template": {
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
                        }
                    },
                }
            ],
        },
    }


def launch_k8s_workflow(
    config: dict | WorkflowConfig, interactive: bool = False, attach: bool = True, args: dict | None = None
):
    """Launch a k8s controller workflow. Returns a K8sWorkflow object."""
    from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

    if isinstance(config, dict):
        config = WorkflowConfig.model_validate(config)

    if interactive:
        if len(config.steps) != 1:
            raise ValueError("Interactive jobs may only have a single step")

    datastore_root = _resolve_datastore_root()

    # Reclaim expired jobs' S3 artifacts. Kicked off at the very start of launch
    # so it overlaps with everything below (code staging, nix eval, k8s setup --
    # ~5-10s); we block on it at the end. In steady state the sweep finishes
    # first, so that join is ~0s.
    sweep_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    sweep_future = sweep_pool.submit(ttl.sweep_expired, datastore_root)

    with tempfile.TemporaryDirectory() as staging_dir:
        staging_dir = Path(staging_dir)

        # Stage code and resolve nix closures first, before any S3/kube/secrets
        # setup below -- nix eval is the step most likely to fail (bad flake,
        # missing store path), so it should fail fast and cheaply rather than
        # after that other setup has already run. The general user workspace
        # stays as the cheap symlink tree; nix-mode roles get their own copied,
        # content-addressed source tree linked into staging under
        # nix-workspaces/<digest>/workspace so eval and the in-cluster build see
        # identical bytes.
        local_code_dest = None
        if config.code is not None:
            local_code_dest = staging_dir / "workspace"
            symlink(Path(config.code.path), local_code_dest, exclude=config.code.exclude, include=config.code.include)

        config = process_nix(config, staged_code_dir=local_code_dest, staging_dir=staging_dir)

        s3_creds = _get_s3_creds()

        job_info = _generate_job_info(datastore_root=datastore_root)
        workflow_id = job_info["id"]
        ttl.write_ttl_marker(datastore_root, workflow_id, config.artifact_ttl)

        workflow_secrets = _create_workflow_secrets(config, workflow_id, s3_creds)

        service_account = _user_config.service_account or detect_service_account(config.namespace)

        # Create assets dir upfront so _package_assets can write dag.json there
        (staging_dir / "assets").mkdir(parents=True, exist_ok=True)

        _package_assets(
            config=config,
            args=args,
            job_info=job_info,
            staging_dir=staging_dir,
            workflow_name=workflow_id,
            workflow_secrets=workflow_secrets,
            interactive=interactive,
        )

    _create_secrets(workflow_id, s3_creds, config)

    jobset_manifest = _build_controller_jobset(
        workflow_id=workflow_id,
        config=config,
        job_info=job_info,
        workflow_secrets=workflow_secrets,
        datastore_root=datastore_root,
        interactive=interactive,
        service_account=service_account,
    )

    k8s_custom = kube.custom_objects
    try:
        k8s_custom.create_namespaced_custom_object(
            group="jobset.x-k8s.io",
            version="v1alpha2",
            plural="jobsets",
            namespace=config.namespace,
            body=jobset_manifest,
        )
    except kubernetes.client.exceptions.ApiException as e:
        if _user_config.service_account:
            hint = (
                f"'{service_account}' was set explicitly via the `service_account` config option "
                "(or SEEKRCHAIN_SERVICE_ACCOUNT) — verify it exists in this namespace and has the "
                "required RBAC (see `chain install-sa`)."
            )
        else:
            hint = (
                f"'{service_account}' was auto-detected but may lack permissions. Run\n\n"
                f"    chain install-sa | kubectl apply -n {config.namespace} -f -\n\n"
                "or set the `service_account` config option to use a specific ServiceAccount."
            )
        raise RuntimeError(
            f"Failed to launch controller JobSet using ServiceAccount {service_account!r} "
            f"in namespace {config.namespace!r}: {e.reason} (status={e.status}).\n\n{hint}"
        ) from e
    logger.info(f"Launched controller JobSet: {workflow_id}")

    # Block on the background TTL sweep started at launch; ideally already done.
    start = time.monotonic()
    try:
        reclaimed = sweep_future.result()
    except Exception as e:
        logger.warning("TTL sweep failed: %s", e)
        reclaimed = 0
    finally:
        sweep_pool.shutdown(wait=False)
    if reclaimed or (dt := time.monotonic() - start) > 0.1:
        logger.info("Reclaimed %d expired job(s); blocked %.1fs on TTL sweep", reclaimed, dt)

    workflow = K8sWorkflow(id=workflow_id, namespace=config.namespace)

    if interactive and attach:
        workflow.attach()

    return workflow
