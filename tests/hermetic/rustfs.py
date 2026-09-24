#!/usr/bin/env python3
"""Hermetic RustFS container management for integration testing."""

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

# In CI each job gets its own RustFS instance so concurrent pipelines do not collide.
_CI_JOB_ID = os.environ.get("CI_JOB_ID", "")
_CLUSTER_SUFFIX = f"-{_CI_JOB_ID}" if _CI_JOB_ID else ""
_IN_CI = bool(os.environ.get("CI"))

# RustFS must join the k3d Docker network, so use docker when available.
# Fall back to podman only if docker is not installed.
_RUNTIME = "docker" if shutil.which("docker") else "podman"

RUSTFS_CONTAINER_NAME = f"seekr-hermetic{_CLUSTER_SUFFIX}-rustfs"
RUSTFS_IMAGE = "rustfs/rustfs:1.0.0"
RUSTFS_NETWORK = f"k3d-seekr-hermetic{_CLUSTER_SUFFIX}"
RUSTFS_BUCKET = "seekr-chain-test"
RUSTFS_ACCESS_KEY = "seekrchain"
RUSTFS_SECRET_KEY = "seekr-chain-hermetic-secret"
# Locally: fixed port for deterministic reuse. CI: runtime-assigned (see _query_host_port).
RUSTFS_HOST_PORT = None if _IN_CI else 19000
RUSTFS_LOCK_PATH = Path(tempfile.gettempdir()) / f"{RUSTFS_CONTAINER_NAME}.lock"


@dataclass
class S3ServiceInfo:
    host_port: int
    bucket: str
    endpoint_url_local: str
    endpoint_url_pod: str
    access_key: str
    secret_key: str


class HermeticRustFS:
    """Manage a RustFS container for hermetic integration tests."""

    def _container_exists(self) -> bool:
        result = subprocess.run(
            [_RUNTIME, "ps", "-a", "--filter", f"name={RUSTFS_CONTAINER_NAME}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return RUSTFS_CONTAINER_NAME in result.stdout

    def _container_is_running(self) -> bool:
        result = subprocess.run(
            [
                _RUNTIME,
                "ps",
                "--filter",
                f"name={RUSTFS_CONTAINER_NAME}",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
        )
        return RUSTFS_CONTAINER_NAME in result.stdout

    def start(self) -> S3ServiceInfo:
        """Start RustFS, create the test bucket, and return connection info.

        Uses a file lock so parallel pytest-xdist workers do not race to create
        the same container simultaneously.
        """
        with open(RUSTFS_LOCK_PATH, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            return self._start_locked()

    def _query_host_port(self) -> int:
        """Ask the container runtime for the host port mapped to RustFS's S3 port."""
        result = subprocess.run(
            [_RUNTIME, "port", RUSTFS_CONTAINER_NAME, "9000"],
            check=True,
            capture_output=True,
            text=True,
        )
        # Output is like "0.0.0.0:32789" or "[::]:32789".
        return int(result.stdout.strip().rsplit(":", 1)[1])

    def _info(self, host_port: int) -> S3ServiceInfo:
        endpoint_url_local = f"http://localhost:{host_port}"
        return S3ServiceInfo(
            host_port=host_port,
            bucket=RUSTFS_BUCKET,
            endpoint_url_local=endpoint_url_local,
            endpoint_url_pod=f"http://{self._get_container_ip()}:9000",
            access_key=RUSTFS_ACCESS_KEY,
            secret_key=RUSTFS_SECRET_KEY,
        )

    def _start_locked(self) -> S3ServiceInfo:
        """Actual RustFS startup logic, called under the file lock."""
        if self._container_is_running():
            host_port = RUSTFS_HOST_PORT or self._query_host_port()
            info = self._info(host_port)
            try:
                self._wait_for_health(info.endpoint_url_local, timeout=5)
                self._create_bucket(info.endpoint_url_local)
                return info
            except TimeoutError:
                pass  # Unhealthy: recreate it below.

        if self._container_exists():
            subprocess.run([_RUNTIME, "rm", "-f", RUSTFS_CONTAINER_NAME])

        # In CI, let the runtime pick a free port to avoid collisions.
        port_flag = f"{RUSTFS_HOST_PORT}:9000" if RUSTFS_HOST_PORT else ":9000"
        print(f"[hermetic] Starting RustFS (port mapping {port_flag})...", file=sys.stderr)
        subprocess.run(
            [
                _RUNTIME,
                "run",
                "-d",
                "--name",
                RUSTFS_CONTAINER_NAME,
                "--network",
                RUSTFS_NETWORK,
                "-p",
                port_flag,
                "-e",
                f"RUSTFS_ACCESS_KEY={RUSTFS_ACCESS_KEY}",
                "-e",
                f"RUSTFS_SECRET_KEY={RUSTFS_SECRET_KEY}",
                RUSTFS_IMAGE,
                "/data",
            ],
            check=True,
        )

        host_port = RUSTFS_HOST_PORT or self._query_host_port()
        info = self._info(host_port)
        self._wait_for_health(info.endpoint_url_local)
        self._create_bucket(info.endpoint_url_local)
        print(
            f"[hermetic] RustFS ready: local={info.endpoint_url_local}, pod={info.endpoint_url_pod}",
            file=sys.stderr,
        )
        return info

    def stop(self):
        """Remove the RustFS container."""
        print("[hermetic] Stopping RustFS...", file=sys.stderr)
        subprocess.run([_RUNTIME, "rm", "-f", RUSTFS_CONTAINER_NAME])

    def _wait_for_health(self, endpoint_url: str, timeout: int = 60):
        """Poll RustFS's S3 health endpoint until it returns 200."""
        deadline = time.time() + timeout
        url = f"{endpoint_url}/health"
        while time.time() < deadline:
            try:
                if requests.get(url, timeout=2).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(1)
        raise TimeoutError(f"RustFS did not become healthy within {timeout}s")

    def _get_container_ip(self) -> str:
        """Get RustFS's IP on the k3d Docker network."""
        # Use index instead of dot notation: the network name contains hyphens.
        fmt = '{{(index .NetworkSettings.Networks "' + RUSTFS_NETWORK + '").IPAddress}}'
        result = subprocess.run(
            [_RUNTIME, "inspect", "--format", fmt, RUSTFS_CONTAINER_NAME],
            check=True,
            capture_output=True,
            text=True,
        )
        ip = result.stdout.strip()
        if not ip:
            raise RuntimeError(f"Could not get RustFS IP on network {RUSTFS_NETWORK}")
        return ip

    def _create_bucket(self, endpoint_url: str):
        """Create the test bucket if it does not already exist."""
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=RUSTFS_ACCESS_KEY,
            aws_secret_access_key=RUSTFS_SECRET_KEY,
            region_name="us-east-1",
        )
        try:
            client.create_bucket(Bucket=RUSTFS_BUCKET)
            print(f"[hermetic] Created bucket: {RUSTFS_BUCKET}", file=sys.stderr)
        except client.exceptions.BucketAlreadyOwnedByYou:
            pass
