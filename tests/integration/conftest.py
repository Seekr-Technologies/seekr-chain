#!/usr/bin/env python3

import datetime
import fcntl
import inspect
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3
import kubernetes
import pytest

import seekr_chain
from seekr_chain.config import MultiRoleStepConfig
from seekr_chain.utils import generate_id

# ---------------------------------------------------------------------------
# Hermetic infrastructure fixtures (k3d cluster + MinIO)
# These are intentionally scoped to tests/integration/ so that running
# pytest tests/unit does not touch any cluster or container infrastructure.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _podman_socket():
    """Start the podman socket service so k3d can reach it (podman-in-podman setup).

    Only active when the podman-socket-start helper is present (i.e., inside the
    hatchery sandbox). Sets DOCKER_HOST so k3d picks up the socket automatically.
    """
    helper = Path("/usr/local/bin/podman-socket-start")
    if not helper.exists():
        yield
        return

    socket_path = Path("/tmp/podman-hermetic.sock")
    if not socket_path.exists():
        subprocess.run(["sudo", "-n", str(helper)], check=False)
        # Brief wait; the helper loops until the socket appears before returning
        for _ in range(20):
            if socket_path.exists():
                break
            time.sleep(0.2)

    os.environ.setdefault("DOCKER_HOST", f"unix://{socket_path}")
    yield


@pytest.fixture(scope="session")
def hermetic_flag(request):
    """Returns True when running in hermetic mode (the default).
    False when --real-cluster or --gpu is passed."""
    if request.config.getoption("--gpu"):
        return False  # --gpu implies real cluster
    return not request.config.getoption("--real-cluster")


# --- xdist-safe shared resource ref-counting ---
# With pytest-xdist each worker gets its own session, so session-scoped fixture
# teardown fires per-worker.  We use an atomic file counter so only the *last*
# worker to finish tears down the shared k3d cluster and MinIO container.
_WORKER_COUNT_PATH = Path(tempfile.gettempdir()) / "seekr-hermetic-worker-count"
_WORKER_COUNT_LOCK = Path(tempfile.gettempdir()) / "seekr-hermetic-worker-count.lock"


def _worker_count_inc() -> int:
    """Atomically increment the active-worker counter. Returns the new value."""
    with open(_WORKER_COUNT_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        count = int(_WORKER_COUNT_PATH.read_text()) if _WORKER_COUNT_PATH.exists() else 0
        count += 1
        _WORKER_COUNT_PATH.write_text(str(count))
        return count


def _worker_count_dec() -> int:
    """Atomically decrement the active-worker counter. Returns the new value."""
    with open(_WORKER_COUNT_LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        count = int(_WORKER_COUNT_PATH.read_text()) if _WORKER_COUNT_PATH.exists() else 1
        count = max(0, count - 1)
        _WORKER_COUNT_PATH.write_text(str(count))
        return count


@pytest.fixture(scope="session")
def k3d_cluster(_podman_socket, hermetic_flag):
    """Session fixture: creates a k3d cluster in hermetic mode (default), yields kubeconfig Path.

    With pytest-xdist, each worker has its own session, so this fixture runs
    per-worker. HermeticCluster.create() uses a file lock internally to ensure
    only one worker creates the cluster; others reuse it.
    """
    if not hermetic_flag:
        yield None
        return

    from hermetic.cluster import HermeticCluster
    from hermetic.minio import HermeticMinio

    cluster = HermeticCluster()
    kubeconfig = cluster.create()
    _worker_count_inc()
    yield kubeconfig
    remaining = _worker_count_dec()
    if remaining == 0:
        if os.environ.get("CI"):
            # In CI, tear down everything to free resources.
            HermeticMinio().stop()
            cluster.destroy()
        else:
            # Locally, keep the cluster alive for fast re-runs and debugging.
            print("[hermetic] Keeping cluster alive for re-use (set CI=1 to tear down).", file=sys.stderr)


@pytest.fixture(scope="session")
def minio_service(hermetic_flag, k3d_cluster):
    """Session fixture: starts MinIO in hermetic mode (default), yields MinioInfo.

    Same file-lock pattern as k3d_cluster — only one worker creates the
    MinIO container; others reuse it.
    """
    if not hermetic_flag:
        yield None
        return

    from hermetic.minio import HermeticMinio

    minio = HermeticMinio()
    info = minio.start()
    yield info
    # Cleanup handled by k3d_cluster teardown (last worker stops MinIO + destroys cluster).
    # Locally, persist for fast re-runs (clean up manually: docker rm -f seekr-hermetic-minio).


@pytest.fixture(scope="session")
def gpu_image():
    """GPU-enabled PyTorch image for integration tests.

    Override with SEEKR_CHAIN_GPU_IMAGE env var to use a private registry image.
    """
    return os.environ.get(
        "SEEKR_CHAIN_GPU_IMAGE",
        "rocm/pytorch:rocm6.4.1_ubuntu24.04_py3.12_pytorch_release_2.6.0",
    )


@pytest.fixture(scope="session")
def datastore_root(hermetic_flag):
    """
    Datastore root for integration tests.
    """
    if hermetic_flag:
        return "s3://seekr-chain-test/seekr-chain/"
    return "s3://seekr-ml-taw/seekr-chain/"


@pytest.fixture(scope="session")
def test_id():
    _test_id = generate_id(N=4)
    print(f"Running with test id: {_test_id}")
    return _test_id


@pytest.fixture
def test_name(request, unique_test_name, test_id):
    name = f"chain-{test_id}"
    return name


@pytest.fixture
def test_code_dir():
    return Path(__file__).parent.parent / "test_code"


@pytest.fixture(scope="session")
def v1_api(k3d_cluster):
    if k3d_cluster is not None:
        kubernetes.config.load_kube_config(config_file=str(k3d_cluster))
    else:
        kubernetes.config.load_kube_config()
    v1 = kubernetes.client.CoreV1Api()
    return v1


@pytest.fixture
def s3_client(minio_service):
    if minio_service is not None:
        return boto3.client(
            "s3",
            endpoint_url=minio_service.endpoint_url_local,
            aws_access_key_id=minio_service.access_key,
            aws_secret_access_key=minio_service.secret_key,
            region_name="us-east-1",
        )
    return boto3.client("s3")


@pytest.fixture
def job_name(request):
    out = ""
    if cls_name := request.node.cls.__name__ if request.node.cls else None:
        out += cls_name + "-"

    out += request.node.name

    out = out.lower().replace("test", "")

    out = re.sub(r"[^a-z0-9-]+", "-", out, flags=re.I)
    out = re.sub(r"-{2,}", "-", out, flags=re.I)

    out = "test-" + out

    return out


@pytest.fixture(autouse=True)
def patch_configs_for_testing(job_name, datastore_root, monkeypatch, hermetic_flag, minio_service, k3d_cluster):
    # Get the real original function (in case it's already been wrapped)
    original = inspect.unwrap(seekr_chain.launch_argo_workflow)

    # Ensure subprocesses (e.g. `chain submit` in test_examples.py) also see the
    # datastore root without needing it explicitly in their config files.
    if datastore_root is not None:
        monkeypatch.setenv("SEEKRCHAIN_DATASTORE_ROOT", datastore_root)

    if hermetic_flag and minio_service is not None:
        # Route test-runner boto3 and aws-cli to MinIO
        monkeypatch.setenv("AWS_ENDPOINT_URL", minio_service.endpoint_url_local)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", minio_service.access_key)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", minio_service.secret_key)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    if hermetic_flag and k3d_cluster is not None:
        # Route argo CLI and kubernetes client to k3d cluster
        monkeypatch.setenv("KUBECONFIG", str(k3d_cluster))

    def wrapper(
        *args,
        _job_name=job_name,
        _orig=original,
        _hermetic=hermetic_flag,
        _minio=minio_service,
        **kwargs,
    ):
        config = kwargs.get("config") or args[0]
        config.name = _job_name
        config.ttl = datetime.timedelta(hours=1)
        if _hermetic:
            config.logging.upload_timeout = datetime.timedelta(seconds=30)
        if config.code:
            config.code.exclude = config.code.exclude + [".cache", "docs"]
        if _hermetic and _minio is not None:
            # Inject pod-side S3 endpoint secrets so init containers and log sidecar
            # can reach MinIO from within the k3d cluster.
            # FB_S3_ENDPOINT must include the http:// prefix — fluent-bit passes it
            # to the AWS SDK as endpointOverride, which defaults to HTTPS when no
            # protocol is specified (causing connection failure against MinIO).
            hermetic_secrets = {
                "AWS_ENDPOINT_URL": _minio.endpoint_url_pod,
                "FB_S3_ENDPOINT": _minio.endpoint_url_pod,
                "AWS_DEFAULT_REGION": "us-east-1",
            }
            if config.secrets is None:
                config.secrets = {}
            config.secrets = {**hermetic_secrets, **config.secrets}

        if _hermetic:
            # Hermetic tests run trivial scripts — use minimal resource requests
            # so many tests can schedule in parallel on the lightweight k3d cluster.
            for step in config.steps:
                roles = step.roles if isinstance(step, MultiRoleStepConfig) else [step]
                for role in roles:
                    if not role.resources.gpus_per_node:
                        role.resources.cpus_per_node = "250m"
                        role.resources.mem_per_node = "256Mi"
                        role.resources.ephemeral_storage_per_node = "1Gi"

        return _orig(*args, **kwargs)

    monkeypatch.setattr(seekr_chain, "launch_argo_workflow", wrapper)
