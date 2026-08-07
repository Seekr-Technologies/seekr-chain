#!/usr/bin/env python3
"""
Single entry point for every Kubernetes API client seekr-chain uses.

Building a client parses the kubeconfig and sets up TLS, which is wasted work
when repeated — and the clients themselves are stateless and safe to share.
So each accessor caches its client for the life of the process. Nothing else
in the package should call ``kubernetes.config.*`` or ``kubernetes.client.*Api()``
directly; ``tests/unit/test_k8s_api.py`` has a guard test that enforces this.

The one sanctioned exception is ``backends/k8s/resources/controller.py``, which
ships standalone into the controller pod and cannot import ``seekr_chain``.

Only out-of-cluster (kubeconfig) auth is handled here. The single in-cluster
consumer is that same standalone controller, so there is nothing for a
``load_incluster_config`` path to serve.

Downstream code keeps taking clients as explicit arguments (see
``workflow_state.py``); this module owns *construction*, not plumbing.
"""

import logging
import os
from functools import lru_cache

import kubernetes

logger = logging.getLogger(__name__)


@lru_cache()
def load_kubeconfig() -> None:
    """Load kubeconfig once with a friendly error on failure."""
    try:
        kubernetes.config.load_kube_config(config_file=os.environ.get("KUBECONFIG"))
    except kubernetes.config.ConfigException as e:
        raise RuntimeError(
            f"Failed to load Kubernetes config: {e}\n\n"
            "Ensure a valid kubeconfig is available:\n"
            "  - Set the KUBECONFIG environment variable, or\n"
            "  - Place a config file at ~/.kube/config"
        ) from e


@lru_cache()
def get_core_v1_api() -> kubernetes.client.CoreV1Api:
    load_kubeconfig()
    return kubernetes.client.CoreV1Api()


@lru_cache()
def get_batch_v1_api() -> kubernetes.client.BatchV1Api:
    load_kubeconfig()
    return kubernetes.client.BatchV1Api()


@lru_cache()
def get_custom_objects_api() -> kubernetes.client.CustomObjectsApi:
    load_kubeconfig()
    return kubernetes.client.CustomObjectsApi()


@lru_cache()
def default_namespace() -> str:
    """Namespace from the active kubeconfig context, or ``"default"``.

    Only consulted when no namespace was given explicitly — the config's own
    ``namespace`` option and ``--namespace`` both take precedence.
    """
    load_kubeconfig()
    _, active_ctx = kubernetes.config.list_kube_config_contexts(config_file=os.environ.get("KUBECONFIG"))
    return active_ctx["context"].get("namespace", "default")


def new_watch() -> kubernetes.watch.Watch:
    """Build a fresh ``Watch``.

    Deliberately uncached, unlike the API clients: a ``Watch`` carries
    per-stream state, and callers construct a new one on every reconnect.
    Routed through this module so watches share the clients' patch seam.
    """
    load_kubeconfig()
    return kubernetes.watch.Watch()


def reset() -> None:
    """Drop every cached client and force a reload on next access.

    For tests, so process-lifetime singletons don't leak between them. Also the
    only supported way to pick up a ``KUBECONFIG`` change mid-process.
    """
    for fn in (
        load_kubeconfig,
        get_core_v1_api,
        get_batch_v1_api,
        get_custom_objects_api,
        default_namespace,
    ):
        fn.cache_clear()
