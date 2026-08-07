"""
Unit tests for seekr_chain.k8s_api.

Covers the caching contract (clients are process-lifetime singletons, watches
are not) and includes a guard test that keeps client construction from drifting
back out of the module.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from seekr_chain import k8s_api

_SRC_ROOT = Path(k8s_api.__file__).parent

# The only files allowed to build Kubernetes clients themselves. controller.py
# ships standalone into the controller pod, where seekr_chain is not installed.
_CONSTRUCTION_ALLOWED = {
    Path("k8s_api.py"),
    Path("backends/k8s/resources/controller.py"),
}

_RAW_CONSTRUCTION = re.compile(
    r"client\.\w*Api\(\)|load_kube_config\(|load_incluster_config\(|watch\.Watch\(",
)


@pytest.fixture
def no_kubeconfig_load():
    """Neutralize kubeconfig loading so accessors work with no cluster present."""
    with patch("kubernetes.config.load_kube_config"):
        yield


ACCESSORS = [
    "get_core_v1_api",
    "get_batch_v1_api",
    "get_custom_objects_api",
]


class TestClientCaching:
    @pytest.mark.parametrize("accessor", ACCESSORS)
    def test_returns_same_instance(self, accessor, no_kubeconfig_load):
        get = getattr(k8s_api, accessor)
        assert get() is get()

    @pytest.mark.parametrize("accessor", ACCESSORS)
    def test_reset_forces_reconstruction(self, accessor, no_kubeconfig_load):
        get = getattr(k8s_api, accessor)
        first = get()
        k8s_api.reset()
        assert get() is not first

    def test_kubeconfig_loaded_once_across_accessors(self):
        with patch("kubernetes.config.load_kube_config") as mock_load:
            k8s_api.get_core_v1_api()
            k8s_api.get_batch_v1_api()
            k8s_api.get_custom_objects_api()
            k8s_api.get_core_v1_api()
        assert mock_load.call_count == 1

    def test_accessors_return_distinct_client_types(self, no_kubeconfig_load):
        clients = {type(getattr(k8s_api, name)()) for name in ACCESSORS}
        assert len(clients) == len(ACCESSORS)


class TestLoadKubeconfig:
    def test_honors_kubeconfig_env(self, monkeypatch):
        monkeypatch.setenv("KUBECONFIG", "/nowhere/config")
        with patch("kubernetes.config.load_kube_config") as mock_load:
            k8s_api.load_kubeconfig()
        mock_load.assert_called_once_with(config_file="/nowhere/config")

    def test_passes_none_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("KUBECONFIG", raising=False)
        with patch("kubernetes.config.load_kube_config") as mock_load:
            k8s_api.load_kubeconfig()
        mock_load.assert_called_once_with(config_file=None)


class TestDefaultNamespace:
    def test_reads_active_context(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "team-ml"}}),
        ):
            assert k8s_api.default_namespace() == "team-ml"

    def test_falls_back_when_context_has_no_namespace(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {}}),
        ):
            assert k8s_api.default_namespace() == "default"

    def test_is_cached(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "team-ml"}}),
        ) as mock_contexts:
            k8s_api.default_namespace()
            k8s_api.default_namespace()
        assert mock_contexts.call_count == 1


class TestNewWatch:
    def test_returns_a_fresh_instance_each_call(self, no_kubeconfig_load):
        """Watches carry per-stream state, so unlike the clients they aren't cached."""
        assert k8s_api.new_watch() is not k8s_api.new_watch()


class TestReset:
    def test_clears_every_cached_accessor(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "ns"}}),
        ):
            k8s_api.default_namespace()
            for name in ACCESSORS:
                getattr(k8s_api, name)()

            k8s_api.reset()

            for name in ACCESSORS:
                fn = getattr(k8s_api, name)
                assert fn.cache_info().currsize == 0, name
            assert k8s_api.load_kubeconfig.cache_info().currsize == 0
            assert k8s_api.default_namespace.cache_info().currsize == 0


class TestErrorWrapping:
    def test_config_exception_becomes_actionable_runtime_error(self):
        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("boom"),
        ):
            with pytest.raises(RuntimeError, match="KUBECONFIG"):
                k8s_api.get_batch_v1_api()

    def test_failed_load_is_not_cached_as_success(self):
        """A failed load must keep raising, not leave a half-built client behind."""
        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("boom"),
        ):
            with pytest.raises(RuntimeError):
                k8s_api.get_core_v1_api()
            with pytest.raises(RuntimeError):
                k8s_api.get_core_v1_api()


class TestNoRawConstructionOutsideK8sApi:
    """Guard against client construction drifting back out of k8s_api.

    Centralization only pays off if it holds: a stray ``client.CoreV1Api()``
    reintroduces the per-call kubeconfig parse and the ordering bugs that come
    with relying on some other call site having loaded the config first.
    """

    def test_only_allowed_files_construct_clients(self):
        offenders = {}
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            rel = path.relative_to(_SRC_ROOT)
            if rel in _CONSTRUCTION_ALLOWED:
                continue
            hits = _RAW_CONSTRUCTION.findall(path.read_text())
            if hits:
                offenders[str(rel)] = sorted(set(hits))

        assert not offenders, (
            f"Kubernetes clients must be obtained from seekr_chain.k8s_api. Raw construction found in: {offenders}"
        )

    def test_guard_pattern_actually_matches_the_allowed_files(self):
        """A typo'd regex would make the guard above pass vacuously."""
        for rel in _CONSTRUCTION_ALLOWED:
            assert _RAW_CONSTRUCTION.search((_SRC_ROOT / rel).read_text()), rel


class TestConsumersUseTheModule:
    def test_k8s_workflow_builds_all_clients_from_k8s_api(self):
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

        mock_batch = MagicMock()
        mock_batch.read_namespaced_job.return_value.metadata.annotations = {
            "seekr-chain/datastore-root": "s3://bucket/"
        }

        with patch("seekr_chain.backends.k8s.k8s_workflow.k8s_api") as mock_k8s_api:
            mock_k8s_api.get_batch_v1_api.return_value = mock_batch
            mock_k8s_api.default_namespace.return_value = "argo"
            workflow = K8sWorkflow(id="wf-1")

        assert workflow._k8s_v1 is mock_k8s_api.get_core_v1_api.return_value
        assert workflow._k8s_batch is mock_batch
        assert workflow._k8s_custom is mock_k8s_api.get_custom_objects_api.return_value
        assert workflow._namespace == "argo"

    def test_explicit_namespace_skips_the_kubeconfig_lookup(self):
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

        with patch("seekr_chain.backends.k8s.k8s_workflow.k8s_api") as mock_k8s_api:
            mock_k8s_api.get_batch_v1_api.return_value.read_namespaced_job.return_value.metadata.annotations = {
                "seekr-chain/datastore-root": "s3://bucket/"
            }
            workflow = K8sWorkflow(id="wf-1", namespace="explicit")

        assert workflow._namespace == "explicit"
        mock_k8s_api.default_namespace.assert_not_called()
