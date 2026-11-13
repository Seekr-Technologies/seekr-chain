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

import copy
import hashlib
import inspect
import logging
import os
import threading
from functools import lru_cache

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


def _selected_policy(merger, context_name):
    """Snapshot only the selected context/cluster/user, including file provenance."""
    context = merger.config["contexts"].get_with_name(context_name)
    cluster = merger.config["clusters"].get_with_name(context["context"]["cluster"])
    user_name = context["context"].safe_get("user")
    user = (
        merger.config["users"].get_with_name(user_name, safe=True)
        if user_name and merger.config.safe_get("users")
        else None
    )
    policy = copy.deepcopy(
        {
            "context": context.value,
            "cluster": cluster.value,
            "user": user.value if user else None,
            "cluster_base": os.path.abspath(os.path.dirname(cluster.path)),
            "user_base": os.path.abspath(os.path.dirname(user.path)) if user else None,
        }
    )
    # Credential values may rotate; the declared sources, usernames, plugin
    # commands, provider endpoints and connection/trust policy must not change.
    if policy["user"]:
        auth = policy["user"]["user"]
        for name in ("token", "password"):
            if name in auth:
                auth[name] = "<rotating credential>"
        provider = auth.get("auth-provider", {}).get("config", {})
        for name in ("access-token", "id-token", "refresh-token", "expiry"):
            provider.pop(name, None)
    return policy


def _connection_policy(configuration):
    """Include certificate contents: an unchanged filename need not mean unchanged trust."""
    attributes = (
        "host",
        "ssl_ca_cert",
        "verify_ssl",
        "tls_server_name",
        "cert_file",
        "key_file",
        "proxy",
        "proxy_headers",
        "assert_hostname",
    )
    settings = [copy.deepcopy(getattr(configuration, name, None)) for name in attributes]
    for name in ("ssl_ca_cert", "cert_file", "key_file"):
        path = getattr(configuration, name, None)
        if path:
            with open(path, "rb") as stream:
                settings.append(hashlib.sha256(stream.read()).digest())
        else:
            settings.append(None)
    return settings


class _ConfigRecovery:
    """Reload native credentials without rediscovering a cluster or auth-source policy."""

    def __init__(self, config_file, context_name, policy, configuration):
        self.config_file = config_file
        self.context_name = context_name
        self.policy = policy
        self.connection = _connection_policy(configuration)
        self.current = configuration
        self.lock = threading.Lock()

    def refresh(self, failed):
        with self.lock:
            if self.current is not failed:
                return self.current
            try:
                merger = KubeConfigMerger(self.config_file)
                if _selected_policy(merger, self.context_name) != self.policy:
                    raise ValueError("Selected kubeconfig policy changed")
                if _connection_policy(failed) != self.connection:
                    raise ValueError("Connection policy changed")
                # Use the unmodified public loader on this already-read snapshot.
                # It owns precedence, relative paths, exec/provider acquisition,
                # persistence and native refresh hooks. No auth type is inferred.
                fresh = _native_configuration(merger, self.context_name)
                if not _has_credentials(fresh):
                    raise ValueError("Reload produced no usable credentials")
                if _connection_policy(fresh) != self.connection:
                    raise ValueError("Reloaded connection policy changed")
                # Recheck after loading to catch replacement during recovery.
                if _selected_policy(KubeConfigMerger(self.config_file), self.context_name) != self.policy:
                    raise ValueError("Selected kubeconfig policy changed")
            except Exception:
                # YAML/plugin/decoder errors can include credential-bearing input.
                raise RuntimeError(
                    "Kubernetes authentication recovery failed: cannot safely reload the pinned configuration"
                ) from None
            logger.warning("Kubernetes authentication rejected; reloaded pinned credentials")
            self.current = fresh
            return fresh


def _repeatable_json(value):
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_repeatable_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _repeatable_json(item) for key, item in value.items())
    return False


class _RecoveringApiClient(kubernetes.client.ApiClient):
    """One 401 replay at the public authenticated-call boundary.

    Request-local shallow copies share the unchanged HTTP transport, but hold a
    configuration snapshot. Recovery neither mutates in-flight credentials nor
    creates/retires pools, so existing streams remain valid.
    """

    def __init__(self, configuration):
        super().__init__(configuration)
        self.recovery = getattr(configuration, "seekr_auth_recovery", None)

    def call_api(self, *args, **kwargs):
        if self.recovery is None:
            return super().call_api(*args, **kwargs)
        parameters = inspect.signature(kubernetes.client.ApiClient.call_api).bind(self, *args, **kwargs).arguments
        parameters.pop("self")
        if parameters.pop("async_req", None):
            return self.pool.apply_async(self._invoke, (parameters,))
        return self._invoke(parameters)

    def _invoke(self, parameters):
        configuration = self.recovery.current if self.recovery else self.configuration
        # Serialize models once, so retrying never regenerates names or consumes
        # non-repeatable content. Files/forms/custom host/auth overrides aren't replayed.
        body = parameters.get("body")
        if body is not None and hasattr(body, "to_dict"):
            body = self.sanitize_for_serialization(body)
        headers = {key.lower(): value for key, value in (parameters.get("header_params") or {}).items()}
        content_type = headers.get("content-type", "application/json").split(";", 1)[0]
        replay = (
            self.recovery is not None
            and _has_credentials(configuration)
            and (
                body is None
                or content_type
                in (
                    "application/json",
                    "application/json-patch+json",
                    "application/merge-patch+json",
                    "application/strategic-merge-patch+json",
                )
            )
            and parameters.get("auth_settings") == ["BearerToken"]
            and not parameters.get("_request_auth")
            and not parameters.get("_host")
            and not parameters.get("files")
            and not parameters.get("post_params")
            and _repeatable_json(body)
            and not any(
                key.lower() == "authorization"
                for headers in (self.default_headers, parameters.get("header_params") or {})
                for key in headers
            )
        )
        if replay:
            parameters = copy.deepcopy(parameters)
            parameters["body"] = body

        request_client = copy.copy(self)
        request_client.configuration = configuration
        request_client.default_headers = self.default_headers.copy()
        try:
            result = kubernetes.client.ApiClient.call_api(
                request_client, **(copy.deepcopy(parameters) if replay else parameters)
            )
        except kubernetes.client.ApiException as error:
            if error.status != 401 or not replay:
                raise
        else:
            self.last_response = request_client.last_response
            return result
        request_client.configuration = self.recovery.refresh(configuration)
        self.configuration = request_client.configuration
        # A second 401 (or any other error) propagates: never recurse or replay a launch.
        result = kubernetes.client.ApiClient.call_api(request_client, **copy.deepcopy(parameters))
        self.last_response = request_client.last_response
        return result


def _native_configuration(merger, context_name):
    """Native kubeconfig loading/persistence, without globals or private overrides.

    Loading the parsed snapshot prevents a concurrent file replacement from
    introducing a different exec command between validation and acquisition.
    """
    loader = KubeConfigLoader(
        config_dict=merger.config,
        active_context=context_name,
        config_base_path=None,
        config_persister=merger.save_changes,
    )
    configuration = kubernetes.client.Configuration()
    loader.load_and_set(configuration)
    return configuration


def _has_credentials(configuration):
    return any(
        value and str(value).strip().lower() not in ("bearer", "basic") for value in configuration.api_key.values()
    ) or bool(configuration.cert_file and configuration.key_file)


def _load_configuration(config_file=None):
    config_file = os.pathsep.join(
        os.path.abspath(os.path.expanduser(path))
        for path in (config_file if config_file is not None else KUBE_CONFIG_DEFAULT_LOCATION).split(os.pathsep)
        if path
    )
    merger = KubeConfigMerger(config_file)
    if merger.config is None:
        raise kubernetes.config.ConfigException("Invalid kube-config file. No configuration found.")
    context_name = merger.config["current-context"]
    policy = _selected_policy(merger, context_name)
    configuration = _native_configuration(merger, context_name)
    if _selected_policy(KubeConfigMerger(config_file), context_name) != policy:
        raise RuntimeError("Kubernetes configuration policy changed during loading")
    configuration.seekr_auth_recovery = _ConfigRecovery(config_file, context_name, policy, configuration)
    return configuration


@lru_cache()
def load_kubeconfig() -> kubernetes.client.Configuration:
    """Return the cached provider-owned config, without changing global defaults.

    Native authentication/refresh behavior is retained. Eligible HTTP-401 calls
    reload credentials once without replacing global defaults or transports.
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
        return kubernetes.client.CoreV1Api(_RecoveringApiClient(load_kubeconfig()))

    @_lazy
    def batch_v1(self) -> kubernetes.client.BatchV1Api:
        return kubernetes.client.BatchV1Api(_RecoveringApiClient(load_kubeconfig()))

    @_lazy
    def custom_objects(self) -> kubernetes.client.CustomObjectsApi:
        return kubernetes.client.CustomObjectsApi(_RecoveringApiClient(load_kubeconfig()))

    @_lazy
    def namespace(self) -> str:
        """Namespace from the active kubeconfig context, or ``"default"``.

        Only consulted when no namespace was given explicitly — the config's own
        ``namespace`` option and ``--namespace`` both take precedence.
        """
        configuration = load_kubeconfig()
        recovery = getattr(configuration, "seekr_auth_recovery", None)
        if recovery is not None:
            return recovery.policy["context"]["context"].get("namespace", "default")
        _, active_ctx = kubernetes.config.list_kube_config_contexts(config_file=os.environ.get("KUBECONFIG"))
        return active_ctx["context"].get("namespace", "default")

    def refresh_auth(self) -> None:
        """Refresh after a watch ERROR event, outside the HTTP-call boundary."""
        configuration = load_kubeconfig()
        recovery = getattr(configuration, "seekr_auth_recovery", None)
        if recovery is not None:
            recovery.refresh(recovery.current)

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
