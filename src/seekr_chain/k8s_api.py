#!/usr/bin/env python3
"""
Single entry point for every Kubernetes API client seekr-chain uses.

Building a client parses the kubeconfig and sets up TLS, which is wasted work
when repeated — and the clients are safe to share, so ``kube`` builds each one
once and hands out the same instance forever. Nothing else in the package should
call ``kubernetes.config.*`` or ``kubernetes.client.*Api()`` directly;
``tests/unit/backends/k8s/test_k8s_api.py`` has a guard test that enforces this.

The one sanctioned exception is ``backends/k8s/resources/controller/``, which
ships standalone into the controller pod and cannot import ``seekr_chain``.

Only out-of-cluster (kubeconfig) auth is handled here. The single in-cluster
consumer is that same standalone controller, so there is nothing for a
``load_incluster_config`` path to serve.

Usage::

    from seekr_chain.k8s_api import kube

    kube.core_v1.list_namespaced_pod(namespace=kube.namespace)

Downstream code keeps taking clients as explicit arguments (see
``workflow_state.py``); this module owns *construction*, not plumbing.
"""

import logging
import os
import threading
from functools import lru_cache, partial

import kubernetes
from kubernetes.config.kube_config import KUBE_CONFIG_DEFAULT_LOCATION, KubeConfigLoader, KubeConfigMerger

logger = logging.getLogger(__name__)

# Guards first-time construction so concurrent callers share one client instead
# of each building their own (launch fans work out across threads). Reentrant
# because the accessors call load_kubeconfig() while already holding it.
_lock = threading.RLock()


class _lazy:
    """Thread-safe ``cached_property``.

    ``functools.cached_property`` drops its lock as of 3.12, so two threads
    racing a first access can each run the factory. Values are cached in the
    instance ``__dict__``, same as ``cached_property``, which is what lets
    ``reset()`` clear everything with one call — and lets tests inject a fake by
    writing that key.
    """

    def __init__(self, factory):
        self._factory = factory
        self.__doc__ = factory.__doc__

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        # A non-data descriptor loses to the instance __dict__, so once the
        # value is cached this never runs again — the lock costs nothing on the
        # hot path.
        with _lock:
            if self._name not in obj.__dict__:
                obj.__dict__[self._name] = self._factory(obj)
            return obj.__dict__[self._name]


def _read_token_file(path):
    """Reopen by path: projected volumes replace symlinks, not file contents."""
    try:
        with open(path) as stream:
            token = stream.read().strip()
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot read Kubernetes token file") from None
    if not token:
        raise RuntimeError("Kubernetes token file is empty")
    return "Bearer " + token


def _refresh_token_file(configuration, *, path: str, key: str):
    """Reread the token file before authenticating a request."""
    configuration.api_key[key] = _read_token_file(path)


class _TokenFileLoader(KubeConfigLoader):
    """Extend only the upstream tokenFile branch, retaining auth precedence.

    These protected loader seams let upstream select the active user and auth
    source (auth-provider wins over token, which wins over exec). Keep coverage
    against supported Kubernetes versions when updating the dependency.
    """

    def _load_user_token(self):
        if self._user and "token" not in self._user and "tokenFile" in self._user:
            self._token_file = os.path.normpath(
                os.path.join(self._get_base_path(self._user.path), self._user["tokenFile"])
            )
            self.token = _read_token_file(self._token_file)
            return True
        return super()._load_user_token()

    def _set_config(self, configuration):
        super()._set_config(configuration)
        if hasattr(self, "_token_file"):
            # Kubernetes 36 renamed this key; use the one upstream populated.
            key = "BearerToken" if "BearerToken" in configuration.api_key else "authorization"
            configuration.refresh_api_key_hook = partial(_refresh_token_file, path=self._token_file, key=key)


def _load_configuration(config_file=None):
    # Match load_kube_config's merging, relative paths and credential persistence,
    # but never mutate Configuration's process-global default.
    merger = KubeConfigMerger(config_file if config_file is not None else KUBE_CONFIG_DEFAULT_LOCATION)
    if merger.config is None:
        raise kubernetes.config.ConfigException("Invalid kube-config file. No configuration found.")
    loader = _TokenFileLoader(config_dict=merger.config, config_base_path=None, config_persister=merger.save_changes)
    configuration = kubernetes.client.Configuration()
    loader.load_and_set(configuration)
    return configuration


@lru_cache()
def load_kubeconfig() -> kubernetes.client.Configuration:
    """Return the cached provider-owned config, without changing global defaults.

    Token-file credentials are reread for each authenticated request; other
    authentication sources retain Kubernetes' own refresh behavior.
    """
    with _lock:
        try:
            return _load_configuration(config_file=os.environ.get("KUBECONFIG"))
        except kubernetes.config.ConfigException as e:
            raise RuntimeError(
                f"Failed to load Kubernetes config: {e}\n\n"
                "Ensure a valid kubeconfig is available:\n"
                "  - Set the KUBECONFIG environment variable, or\n"
                "  - Place a config file at ~/.kube/config"
            ) from e


class _KubeApi:
    """Lazily-built, shared Kubernetes clients. Use the module-level ``kube``."""

    @_lazy
    def core_v1(self) -> kubernetes.client.CoreV1Api:
        return kubernetes.client.CoreV1Api(kubernetes.client.ApiClient(load_kubeconfig()))

    @_lazy
    def batch_v1(self) -> kubernetes.client.BatchV1Api:
        return kubernetes.client.BatchV1Api(kubernetes.client.ApiClient(load_kubeconfig()))

    @_lazy
    def custom_objects(self) -> kubernetes.client.CustomObjectsApi:
        return kubernetes.client.CustomObjectsApi(kubernetes.client.ApiClient(load_kubeconfig()))

    @_lazy
    def namespace(self) -> str:
        """Namespace from the active kubeconfig context, or ``"default"``.

        Only consulted when no namespace was given explicitly — the config's own
        ``namespace`` option and ``--namespace`` both take precedence.
        """
        load_kubeconfig()
        _, active_ctx = kubernetes.config.list_kube_config_contexts(config_file=os.environ.get("KUBECONFIG"))
        return active_ctx["context"].get("namespace", "default")

    def new_watch(self) -> kubernetes.watch.Watch:
        """Build a fresh ``Watch``.

        A method rather than one of the cached accessors above: a ``Watch``
        carries per-stream state, and callers construct a new one on every
        reconnect. Routed through here so watches share the clients' patch seam.
        """
        load_kubeconfig()
        return kubernetes.watch.Watch()

    def reset(self) -> None:
        """Drop every cached client and force a reload on next access.

        For tests, so process-lifetime singletons don't leak between them. Also
        the only supported way to pick up a ``KUBECONFIG`` change mid-process.
        """
        with _lock:
            self.__dict__.clear()
            load_kubeconfig.cache_clear()


kube = _KubeApi()
