#!/usr/bin/env python3
"""
HermeticCluster: manages a k3d cluster for hermetic testing.
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

# When running inside GitLab CI, each job gets a unique cluster so concurrent
# pipelines on the same runner don't collide.  Locally we keep a fixed name so
# the cluster persists across test runs for fast iteration.
_CI_JOB_ID = os.environ.get("CI_JOB_ID", "")
_CLUSTER_SUFFIX = f"-{_CI_JOB_ID}" if _CI_JOB_ID else ""

CLUSTER_NAME = f"seekr-hermetic{_CLUSTER_SUFFIX}"
CLUSTER_LOCK_PATH = Path(tempfile.gettempdir()) / f"{CLUSTER_NAME}-cluster.lock"
ARGO_VERSION = "v3.6.7"
JOBSET_VERSION = "v0.7.2"
# Argo's install.yaml hardcodes namespace: argo for its own components.
ARGO_CONTROLLER_NAMESPACE = "argo"
# Namespace where test workflows are submitted (matches WorkflowConfig.namespace in tests).
ARGO_WORKFLOW_NAMESPACE = "argo-workflows"
KUBECONFIG_PATH = Path(tempfile.gettempdir()) / f"{CLUSTER_NAME}-kubeconfig.yaml"

# Images pre-pulled and imported into the k3d cluster before tests run.
# This prevents Docker Hub rate limits from causing silent ImagePullBackOff
# failures mid-test. Add an image here whenever a new one is used in tests.
# Images pre-pulled and imported into every k3d node before tests run.
# Keep this list small — each image consumes space in the k3d node's
# containerd storage, which is limited on CI runners.
# Only include images pulled on every test job (init containers + log sidecar).
# Larger test-job images (ubuntu, python, etc.) pull through Docker Hub
# registry auth configured in registries.yaml.
HERMETIC_IMAGES = [
    "amazon/aws-cli:2.25.11",
    "alpine:3.22.0",
    "busybox:1.37-uclibc",
    "fluent/fluent-bit:2.2-debug",
    "ubuntu:24.04",
    "python:3.12-alpine",
    "python:3.13-alpine",
]


# Grant the Argo workflow-controller SA permission to manage JobSet resources.
# The SA is named 'argo' and lives in the 'argo' namespace (install.yaml default).
_JOBSET_RBAC = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: argo-jobset-role
rules:
- apiGroups: ["jobset.x-k8s.io"]
  resources: ["jobsets", "jobsets/status"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: argo-jobset-rolebinding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: argo-jobset-role
subjects:
- kind: ServiceAccount
  name: argo
  namespace: argo
"""

# Grant the Argo executor pods running in argo-workflows the permissions they
# need to operate. Argo uses the 'default' SA in the workflow namespace unless
# overridden, so we bind it here.
#
# Two bindings are needed:
#   1. argo-cluster-role  — WorkflowTaskResult (report back to controller),
#                           pods/log, configmaps, secrets, etc.
#   2. argo-jobset-role   — create/watch JobSet resources (our step backend)
_WORKFLOW_EXECUTOR_RBAC = """\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: argo-workflows-executor-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: argo-cluster-role
subjects:
- kind: ServiceAccount
  name: default
  namespace: argo-workflows
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: argo-workflows-jobset-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: argo-jobset-role
subjects:
- kind: ServiceAccount
  name: default
  namespace: argo-workflows
"""


def _run(cmd, env=None, input=None, **kwargs):
    """Run a command, raising on failure, printing the command."""
    print(f"[hermetic] {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run(cmd, check=True, env=env, input=input, **kwargs)


def _run_capture(cmd, env=None):
    """Run a command and return stdout."""
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


class HermeticCluster:
    """Manages a k3d cluster for hermetic integration tests."""

    def __init__(self):
        self._check_prerequisites()
        self._registry_config_path: Path | None = None

    def _check_prerequisites(self):
        # Accept either docker or podman as the container runtime (podman-docker
        # provides a 'docker' shim, but check both in case it's not installed).
        has_runtime = shutil.which("docker") is not None or shutil.which("podman") is not None
        missing = [t for t in ("k3d", "kubectl", "argo") if shutil.which(t) is None]
        if not has_runtime:
            missing.insert(0, "docker or podman")
        if missing:
            raise RuntimeError(
                f"Missing required tools for hermetic testing: {', '.join(missing)}\n"
                "Please install them before running hermetic tests.\n"
                "  k3d:    https://k3d.io/#installation\n"
                "  kubectl: https://kubernetes.io/docs/tasks/tools/\n"
                "  argo:   bash scripts/install-argo.sh"
            )

    def _write_registry_config(self) -> Path | None:
        """Write a k3s registries.yaml with Docker Hub credentials, if available.

        Returns the path to the temp file, or None if credentials aren't set.
        """
        username = os.environ.get("DOCKERHUB_USERNAME")
        token = os.environ.get("DOCKERHUB_TOKEN")
        if not username or not token:
            if os.environ.get("CI"):
                raise RuntimeError(
                    "DOCKERHUB_USERNAME and DOCKERHUB_TOKEN must be set in CI to avoid Docker Hub rate limits."
                )
            return None

        content = f'configs:\n  "docker.io":\n    auth:\n      username: {username}\n      password: {token}\n'
        path = Path(tempfile.gettempdir()) / f"{CLUSTER_NAME}-registries.yaml"
        path.write_text(content)
        print(f"[hermetic] Wrote registry config with Docker Hub credentials to {path}", file=sys.stderr)
        return path

    def _cluster_exists(self) -> bool:
        result = subprocess.run(
            ["k3d", "cluster", "list", "-o", "json"],
            capture_output=True,
            text=True,
        )
        return CLUSTER_NAME in result.stdout

    def _cluster_is_healthy(self, kubeconfig_path: Path) -> bool:
        """Return True if both Argo and JobSet controllers are Available (not just present)."""
        kenv = {**os.environ, "KUBECONFIG": str(kubeconfig_path)}
        for deployment, ns in [
            ("workflow-controller", ARGO_CONTROLLER_NAMESPACE),
            ("jobset-controller-manager", "jobset-system"),
        ]:
            r = subprocess.run(
                [
                    "kubectl",
                    "wait",
                    f"deployment/{deployment}",
                    "-n",
                    ns,
                    "--for=condition=Available",
                    "--timeout=15s",
                ],
                env=kenv,
                capture_output=True,
            )
            if r.returncode != 0:
                return False
        return True

    def create(self, retries: int = 3) -> Path:
        """Create k3d cluster, install Argo + JobSet. Returns kubeconfig Path.

        Uses a file lock so that parallel pytest-xdist workers don't race to
        create the same cluster simultaneously. Retries on transient failures
        (e.g. podman networking flakes during CI).
        """
        for attempt in range(1, retries + 1):
            try:
                with open(CLUSTER_LOCK_PATH, "w") as _lf:
                    fcntl.flock(_lf, fcntl.LOCK_EX)
                    return self._create_locked()
            except (RuntimeError, subprocess.CalledProcessError) as exc:
                if attempt == retries:
                    raise
                print(
                    f"[hermetic] Cluster creation attempt {attempt}/{retries} failed: {exc}\n"
                    f"[hermetic] Retrying in 10s...",
                    file=sys.stderr,
                )
                time.sleep(10)

    def _create_locked(self) -> Path:
        """Actual cluster creation logic, called under the file lock."""
        if self._cluster_exists():
            kubeconfig_data = _run_capture(["k3d", "kubeconfig", "get", CLUSTER_NAME])
            KUBECONFIG_PATH.write_text(kubeconfig_data)
            KUBECONFIG_PATH.chmod(0o600)
            if self._cluster_is_healthy(KUBECONFIG_PATH):
                print(f"[hermetic] Reusing existing cluster. Kubeconfig: {KUBECONFIG_PATH}", file=sys.stderr)
                return KUBECONFIG_PATH
            print("[hermetic] Cluster unhealthy — recreating.", file=sys.stderr)
            subprocess.run(["k3d", "cluster", "delete", CLUSTER_NAME])

        print(f"[hermetic] Creating k3d cluster '{CLUSTER_NAME}'...", file=sys.stderr)

        self._registry_config_path = self._write_registry_config()

        try:
            create_cmd = [
                "k3d",
                "cluster",
                "create",
                CLUSTER_NAME,
                "--servers",
                "1",
                "--agents",
                "1",
                "--no-lb",
                "--k3s-arg",
                "--disable=traefik@server:0",
                "--k3s-arg",
                "--disable=servicelb@server:0",
                "--kubeconfig-update-default=false",
                "--kubeconfig-switch-context=false",
                "--timeout",
                "120s",
            ]
            # In CI, derive unique service/cluster CIDRs from CI_JOB_ID so two
            # concurrent k3s instances don't install overlapping iptables rules.
            if _CI_JOB_ID:
                _cidr_byte = int(hashlib.sha256(_CI_JOB_ID.encode()).hexdigest()[:2], 16) % 200 + 2
                create_cmd += [
                    "--k3s-arg",
                    f"--service-cidr=10.{_cidr_byte}.0.0/16@server:*",
                    "--k3s-arg",
                    f"--cluster-cidr=10.{(_cidr_byte + 100) % 256}.0.0/16@server:*",
                ]
            if self._registry_config_path:
                create_cmd += ["--registry-config", str(self._registry_config_path)]
            _run(create_cmd)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"k3d cluster creation failed (exit code {e.returncode}).\n"
                "Check 'Captured stderr setup' above for the actual k3d error.\n"
                "Common causes:\n"
                "  - Docker/podman not reachable (check DOCKER_HOST)\n"
                "  - TLS cert issues (check DOCKER_TLS_VERIFY, DOCKER_CERT_PATH)\n"
                "  - Missing CAP_NET_ADMIN (if running inside a container)"
            ) from e

        kubeconfig_data = _run_capture(["k3d", "kubeconfig", "get", CLUSTER_NAME])

        # When k3d uses a remote Docker daemon (e.g. Docker-in-Docker in CI),
        # the kubeconfig server address is 0.0.0.0:<port>. That address isn't
        # reachable from the CI container — replace it with the Docker host.
        docker_host = os.environ.get("DOCKER_HOST", "")
        if docker_host:
            parsed = urlparse(docker_host)
            if parsed.hostname:
                kubeconfig_data = kubeconfig_data.replace("0.0.0.0", parsed.hostname)
                print(f"[hermetic] Patched kubeconfig server to {parsed.hostname} (DOCKER_HOST)", file=sys.stderr)

        KUBECONFIG_PATH.write_text(kubeconfig_data)
        KUBECONFIG_PATH.chmod(0o600)

        kenv = {**os.environ, "KUBECONFIG": str(KUBECONFIG_PATH)}

        # Wait for the API server to become reachable. With remote Docker or
        # podman the port mapping may not be immediately available.
        print("[hermetic] Waiting for API server...", file=sys.stderr)
        for attempt in range(30):
            r = subprocess.run(["kubectl", "cluster-info"], env=kenv, capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"API server not reachable after cluster creation.\n{r.stdout}\n{r.stderr}")

        # install.yaml references namespace: argo but doesn't create it.
        print(f"[hermetic] Creating namespace {ARGO_CONTROLLER_NAMESPACE}...", file=sys.stderr)
        subprocess.run(["kubectl", "create", "namespace", ARGO_CONTROLLER_NAMESPACE], env=kenv)

        # install.yaml creates its own resources in the 'argo' namespace — do not pass -n.
        print(
            f"[hermetic] Installing Argo Workflows {ARGO_VERSION} (namespace: {ARGO_CONTROLLER_NAMESPACE})...",
            file=sys.stderr,
        )
        _run(
            [
                "kubectl",
                "apply",
                "-f",
                f"https://github.com/argoproj/argo-workflows/releases/download/{ARGO_VERSION}/install.yaml",
            ],
            env=kenv,
        )

        # Create the namespace where test workflows will be submitted.
        print(f"[hermetic] Creating workflow namespace {ARGO_WORKFLOW_NAMESPACE}...", file=sys.stderr)
        subprocess.run(
            ["kubectl", "create", "namespace", ARGO_WORKFLOW_NAMESPACE],
            env=kenv,
        )  # Ignore failure — namespace may already exist

        print("[hermetic] Applying JobSet RBAC for argo SA...", file=sys.stderr)
        _run(
            ["kubectl", "apply", "-f", "-"],
            env=kenv,
            input=_JOBSET_RBAC.encode(),
        )

        print("[hermetic] Applying executor RBAC for default SA in argo-workflows...", file=sys.stderr)
        _run(
            ["kubectl", "apply", "-f", "-"],
            env=kenv,
            input=_WORKFLOW_EXECUTOR_RBAC.encode(),
        )

        print(f"[hermetic] Installing JobSet {JOBSET_VERSION}...", file=sys.stderr)
        _run(
            [
                "kubectl",
                "apply",
                "--server-side",
                "-f",
                f"https://github.com/kubernetes-sigs/jobset/releases/download/{JOBSET_VERSION}/manifests.yaml",
            ],
            env=kenv,
        )

        # The JobSet manifest references gcr.io/kubebuilder/kube-rbac-proxy which
        # is deprecated and no longer serves images. Patch the deployment to pull
        # from the registry.k8s.io mirror instead.
        print("[hermetic] Patching JobSet kube-rbac-proxy image to registry.k8s.io...", file=sys.stderr)
        _run(
            [
                "kubectl",
                "set",
                "image",
                "deployment/jobset-controller-manager",
                "kube-rbac-proxy=registry.k8s.io/kubebuilder/kube-rbac-proxy:v0.13.1",
                "-n",
                "jobset-system",
            ],
            env=kenv,
        )

        print("[hermetic] Waiting for workflow-controller...", file=sys.stderr)
        _run(
            [
                "kubectl",
                "wait",
                "deployment/workflow-controller",
                "-n",
                ARGO_CONTROLLER_NAMESPACE,
                "--for=condition=Available",
                "--timeout=300s",
            ],
            env=kenv,
        )

        print("[hermetic] Waiting for jobset-controller-manager...", file=sys.stderr)
        try:
            _run(
                [
                    "kubectl",
                    "wait",
                    "deployment/jobset-controller-manager",
                    "-n",
                    "jobset-system",
                    "--for=condition=Available",
                    "--timeout=300s",
                ],
                env=kenv,
            )
        except subprocess.CalledProcessError:
            # Dump diagnostics before re-raising so CI logs show why the controller isn't ready.
            print("[hermetic] jobset-controller-manager not Available — dumping diagnostics:", file=sys.stderr)
            subprocess.run(
                ["kubectl", "describe", "deployment/jobset-controller-manager", "-n", "jobset-system"],
                env=kenv,
            )
            subprocess.run(
                ["kubectl", "get", "pods", "-n", "jobset-system", "-o", "wide"],
                env=kenv,
            )
            subprocess.run(
                ["kubectl", "logs", "deployment/jobset-controller-manager", "-n", "jobset-system", "--tail=50"],
                env=kenv,
            )
            raise

        # Note: Argo v3 uses the 'emissary' executor by default, which communicates
        # via the k8s API and does not need containerd socket access. The old
        # 'containerRuntimeExecutor' configmap field was removed in v3 — patching
        # it crashes the controller. No configmap patch is needed for k3d.

        self._preload_images()

        print(f"[hermetic] Cluster ready. Kubeconfig: {KUBECONFIG_PATH}", file=sys.stderr)
        return KUBECONFIG_PATH

    def _preload_images(self):
        """Pull all hermetic test images and import them into the k3d cluster.

        Pulling upfront means rate limit failures are visible immediately at
        cluster setup time rather than appearing as silent hangs mid-test.

        Each image is pulled, imported, then pruned before moving to the next
        so that only one image's tarball is on disk at a time.
        """
        for image in HERMETIC_IMAGES:
            print(f"[hermetic] Pre-pulling {image}...", file=sys.stderr)
            result = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
            if result.returncode != 0:
                print(
                    f"[hermetic] WARNING: failed to pull {image}:\n{result.stderr.strip()}",
                    file=sys.stderr,
                )
                continue
            _run(["k3d", "image", "import", image, "--cluster", CLUSTER_NAME])
            subprocess.run(["docker", "image", "rm", image], capture_output=True)

    def destroy(self):
        """Delete the k3d cluster."""
        print(f"[hermetic] Destroying k3d cluster '{CLUSTER_NAME}'...", file=sys.stderr)
        subprocess.run(["k3d", "cluster", "delete", CLUSTER_NAME])
        if KUBECONFIG_PATH.exists():
            KUBECONFIG_PATH.unlink()
