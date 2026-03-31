#!/usr/bin/env python3
"""
HermeticMinio: manages a MinIO container for hermetic testing.
"""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import requests

# In CI each job gets its own MinIO so concurrent pipelines don't collide.
_CI_JOB_ID = os.environ.get("CI_JOB_ID", "")
_CLUSTER_SUFFIX = f"-{_CI_JOB_ID}" if _CI_JOB_ID else ""
_IN_CI = bool(os.environ.get("CI"))

# Always prefer docker when available — k3d uses Docker networks, so MinIO must
# join the same runtime to attach to the k3d-created network.
# Fall back to podman only when docker is absent (e.g. podman-only CI environments).
_RUNTIME = "docker" if shutil.which("docker") else "podman"

MINIO_CONTAINER_NAME = f"seekr-hermetic{_CLUSTER_SUFFIX}-minio"
MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2025-01-20T14-49-07Z"
MINIO_NETWORK = f"k3d-seekr-hermetic{_CLUSTER_SUFFIX}"
MINIO_BUCKET = "seekr-chain-test"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
# Locally: fixed port for deterministic reuse.  CI: runtime-assigned (see _query_host_port).
MINIO_HOST_PORT = None if _IN_CI else 19000
MINIO_LOCK_PATH = Path(tempfile.gettempdir()) / f"{MINIO_CONTAINER_NAME}.lock"


@dataclass
class MinioInfo:
    host_port: int
    bucket: str
    endpoint_url_local: str
    endpoint_url_pod: str
    access_key: str
    secret_key: str


class HermeticMinio:
    """Manages a MinIO container for hermetic integration tests."""

    def _container_exists(self) -> bool:
        result = subprocess.run(
            [_RUNTIME, "ps", "-a", "--filter", f"name={MINIO_CONTAINER_NAME}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return MINIO_CONTAINER_NAME in result.stdout

    def _container_is_running(self) -> bool:
        result = subprocess.run(
            [
                _RUNTIME,
                "ps",
                "--filter",
                f"name={MINIO_CONTAINER_NAME}",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
        )
        return MINIO_CONTAINER_NAME in result.stdout

    def start(self) -> MinioInfo:
        """Start MinIO container, create bucket, return connection info.

        Uses a file lock so that parallel pytest-xdist workers don't race to
        create the same container simultaneously.
        """
        with open(MINIO_LOCK_PATH, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            return self._start_locked()

    def _query_host_port(self) -> int:
        """Ask the container runtime which host port is mapped to container port 9000."""
        result = subprocess.run(
            [_RUNTIME, "port", MINIO_CONTAINER_NAME, "9000"],
            check=True,
            capture_output=True,
            text=True,
        )
        # Output is like "0.0.0.0:32789" or "[::]:32789"
        return int(result.stdout.strip().rsplit(":", 1)[1])

    def _start_locked(self) -> MinioInfo:
        """Actual MinIO startup logic, called under the file lock."""
        if self._container_is_running():
            host_port = MINIO_HOST_PORT or self._query_host_port()
            endpoint_url_local = f"http://localhost:{host_port}"
            try:
                self._wait_for_health(endpoint_url_local, timeout=5)
                pod_ip = self._get_container_ip()
                return MinioInfo(
                    host_port=host_port,
                    bucket=MINIO_BUCKET,
                    endpoint_url_local=endpoint_url_local,
                    endpoint_url_pod=f"http://{pod_ip}:9000",
                    access_key=MINIO_ACCESS_KEY,
                    secret_key=MINIO_SECRET_KEY,
                )
            except TimeoutError:
                pass  # unhealthy, fall through to recreate

        if self._container_exists():
            subprocess.run([_RUNTIME, "rm", "-f", MINIO_CONTAINER_NAME])

        # In CI, let the runtime pick a free port to avoid collisions.
        port_flag = f"{MINIO_HOST_PORT}:9000" if MINIO_HOST_PORT else ":9000"
        print(f"[hermetic] Starting MinIO (port mapping {port_flag})...", file=sys.stderr)

        subprocess.run(
            [
                _RUNTIME,
                "run",
                "-d",
                "--name",
                MINIO_CONTAINER_NAME,
                "--network",
                MINIO_NETWORK,
                "-p",
                port_flag,
                "-e",
                f"MINIO_ROOT_USER={MINIO_ACCESS_KEY}",
                "-e",
                f"MINIO_ROOT_PASSWORD={MINIO_SECRET_KEY}",
                MINIO_IMAGE,
                "server",
                "/data",
            ],
            check=True,
        )

        host_port = MINIO_HOST_PORT or self._query_host_port()
        endpoint_url_local = f"http://localhost:{host_port}"
        self._wait_for_health(endpoint_url_local)

        pod_ip = self._get_container_ip()
        endpoint_url_pod = f"http://{pod_ip}:9000"

        self._create_bucket(endpoint_url_local)

        info = MinioInfo(
            host_port=host_port,
            bucket=MINIO_BUCKET,
            endpoint_url_local=endpoint_url_local,
            endpoint_url_pod=endpoint_url_pod,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
        )
        print(
            f"[hermetic] MinIO ready: local={endpoint_url_local}, pod={endpoint_url_pod}",
            file=sys.stderr,
        )
        return info

    def stop(self):
        """Remove the MinIO container."""
        print("[hermetic] Stopping MinIO...", file=sys.stderr)
        subprocess.run([_RUNTIME, "rm", "-f", MINIO_CONTAINER_NAME])

    def _wait_for_health(self, endpoint_url: str, timeout: int = 60):
        """Poll MinIO health endpoint until it returns 200."""
        deadline = time.time() + timeout
        url = f"{endpoint_url}/minio/health/live"
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=2)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(1)
        raise TimeoutError(f"MinIO did not become healthy within {timeout}s")

    def _get_container_ip(self) -> str:
        """Get MinIO's IP on the k3d Docker network."""
        # Use index function instead of dot notation because the network name
        # contains hyphens, which Go templates treat as a minus operator.
        fmt = '{{(index .NetworkSettings.Networks "' + MINIO_NETWORK + '").IPAddress}}'
        result = subprocess.run(
            [_RUNTIME, "inspect", "--format", fmt, MINIO_CONTAINER_NAME],
            check=True,
            capture_output=True,
            text=True,
        )
        ip = result.stdout.strip()
        if not ip:
            raise RuntimeError(f"Could not get MinIO IP on network {MINIO_NETWORK}")
        return ip

    def _create_bucket(self, endpoint_url: str):
        """Create the test bucket in MinIO."""
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            region_name="us-east-1",
        )
        client.create_bucket(Bucket=MINIO_BUCKET)
        print(f"[hermetic] Created bucket: {MINIO_BUCKET}", file=sys.stderr)
