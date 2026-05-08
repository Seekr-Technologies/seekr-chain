#!/usr/bin/env python3
"""
RBAC helpers for the k8s controller backend.

The controller pod needs a ServiceAccount with permissions to:
  - create/get/list/watch/delete JobSets
  - get its own Job (to read UID for ownerReferences)
  - list/get Pods (for get_detailed_state)

seekr-chain auto-detects which ServiceAccount to use by probing the namespace
in order of preference:

  1. ``seekr-chain-controller``  — dedicated SA (from ``chain install``)
  2. ``argo``                    — Argo Workflows SA (already has JobSet perms)

To set up the ``seekr-chain-controller`` SA, run::

    chain install | kubectl apply -n <namespace> -f -
"""

import logging
from pathlib import Path

import kubernetes

logger = logging.getLogger(__name__)

# Probed in order; first match wins.
_CANDIDATE_SERVICE_ACCOUNTS = ["seekr-chain-controller", "argo", "argo-workflows", "argo-workflow"]

_RBAC_YAML_PATH = Path(__file__).parent / "resources" / "rbac.yaml"


def rbac_yaml() -> str:
    """Return the contents of the bundled RBAC YAML template."""
    return _RBAC_YAML_PATH.read_text()


def _sa_exists(core_v1: kubernetes.client.CoreV1Api, namespace: str, name: str) -> bool:
    try:
        core_v1.read_namespaced_service_account(name=name, namespace=namespace)
        return True
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise


def detect_service_account(namespace: str) -> str:
    """Detect which ServiceAccount to use for the controller pod.

    Probes the namespace for known SAs in preference order.  Raises
    ``RuntimeError`` with setup instructions if none are found.

    Returns the name of the ServiceAccount to use.
    """
    core_v1 = kubernetes.client.CoreV1Api()

    for name in _CANDIDATE_SERVICE_ACCOUNTS:
        if _sa_exists(core_v1, namespace, name):
            logger.debug(f"Using ServiceAccount {name!r} in namespace {namespace!r}")
            return name

    candidates = ", ".join(f"'{n}'" for n in _CANDIDATE_SERVICE_ACCOUNTS)
    raise RuntimeError(
        f"No suitable ServiceAccount found in namespace {namespace!r}.\n"
        f"Looked for: {candidates}\n\n"
        "Run the one-time install to create the seekr-chain ServiceAccount and RBAC:\n\n"
        f"    chain install | kubectl apply -n {namespace} -f -\n\n"
        "Or, if Argo Workflows is installed in a different namespace, point seekr-chain\n"
        "at it by setting the correct namespace in your config."
    )
