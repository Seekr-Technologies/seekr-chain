#!/usr/bin/env python3
"""
RBAC helpers for the k8s controller backend.

The controller pod needs a ServiceAccount with permissions to:
  - create/get/list/watch/delete JobSets
  - get its own Job (to read UID for ownerReferences)
  - list/get Pods (for get_detailed_state)

seekr-chain auto-detects which ServiceAccount to use by probing the namespace
in order of preference:

  1. ``seekr-chain-controller``  — dedicated SA (from ``chain install-sa``)
  2. ``argo``                    — Argo Workflows SA (already has JobSet perms)

To set up the ``seekr-chain-controller`` SA, run::

    chain install-sa | kubectl apply -n <namespace> -f -
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


def detect_service_account(namespace: str) -> str:
    """Detect which ServiceAccount to use for the controller pod.

    Probes candidates in preference order with a per-name ``get`` — not a
    namespace-wide ``list`` — since the caller's own kubeconfig identity
    (not the controller SA) is what's used here, and it may only have
    narrow, named-resource RBAC rather than a blanket list grant. Raises
    ``RuntimeError`` with setup instructions if none are found or accessible.
    """
    core_v1 = kubernetes.client.CoreV1Api()

    for name in _CANDIDATE_SERVICE_ACCOUNTS:
        try:
            core_v1.read_namespaced_service_account(name=name, namespace=namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status not in (403, 404):
                raise
        else:
            logger.debug(f"Using ServiceAccount {name!r} in namespace {namespace!r}")
            return name

    candidates = ", ".join(f"'{n}'" for n in _CANDIDATE_SERVICE_ACCOUNTS)
    raise RuntimeError(
        f"No suitable ServiceAccount found in namespace {namespace!r}.\n"
        f"Looked for: {candidates}\n\n"
        "Run the one-time install to create the seekr-chain ServiceAccount and RBAC:\n\n"
        f"    chain install-sa | kubectl apply -n {namespace} -f -\n\n"
        "Or, if Argo Workflows is installed in a different namespace, point seekr-chain\n"
        "at it by setting the correct namespace in your config."
    )
