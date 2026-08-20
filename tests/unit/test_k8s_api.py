"""
Unit tests for seekr_chain.k8s_api.

Covers the caching contract (clients are process-lifetime singletons built once
even under concurrent first access, watches are not cached) and includes a guard
test that keeps client construction from drifting back out of the module.
"""

import re
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from seekr_chain import k8s_api
from seekr_chain.k8s_api import kube

_SRC_ROOT = Path(k8s_api.__file__).parent

# The only files allowed to build Kubernetes clients themselves. controllerlib/watch.py
# ships standalone into the controller pod, where seekr_chain is not installed.
_CONSTRUCTION_ALLOWED = {
    Path("k8s_api.py"),
    Path("backends/k8s/resources/controllerlib/watch.py"),
}

_RAW_CONSTRUCTION = re.compile(
    r"client\.\w*Api\(\)|load_kube_config\(|load_incluster_config\(|watch\.Watch\(",
)

# Cached client accessors. `namespace` is cached too but is a str, so the
# identity-based assertions below don't apply to it.
CLIENTS = ["core_v1", "batch_v1", "custom_objects"]


@pytest.fixture
def no_kubeconfig_load():
    """Neutralize kubeconfig loading so accessors work with no cluster present."""
    with patch("kubernetes.config.load_kube_config"):
        yield


class TestClientCaching:
    @pytest.mark.parametrize("name", CLIENTS)
    def test_returns_same_instance(self, name, no_kubeconfig_load):
        assert getattr(kube, name) is getattr(kube, name)

    @pytest.mark.parametrize("name", CLIENTS)
    def test_reset_forces_reconstruction(self, name, no_kubeconfig_load):
        first = getattr(kube, name)
        kube.reset()
        assert getattr(kube, name) is not first

    def test_kubeconfig_loaded_once_across_accessors(self):
        with patch("kubernetes.config.load_kube_config") as mock_load:
            kube.core_v1
            kube.batch_v1
            kube.custom_objects
            kube.core_v1
        assert mock_load.call_count == 1

    def test_accessors_return_distinct_client_types(self, no_kubeconfig_load):
        assert len({type(getattr(kube, name)) for name in CLIENTS}) == len(CLIENTS)

    def test_values_are_cached_in_instance_dict(self, no_kubeconfig_load):
        """reset() and test injection both rely on the cache living in __dict__."""
        assert "core_v1" not in kube.__dict__
        client = kube.core_v1
        assert kube.__dict__["core_v1"] is client


def _hammer(getter, n=8):
    """Run ``getter`` on n threads released simultaneously; return (results, errors)."""
    results = []
    errors = []
    start = threading.Barrier(n)

    def run():
        try:
            start.wait()
            results.append(getter())
        except Exception as e:  # pragma: no cover - surfaced via `errors`
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class TestThreadSafety:
    """`launch` fans work out across threads, so first access is genuinely concurrent."""

    @pytest.mark.parametrize("name,cls", list(zip(CLIENTS, ["CoreV1Api", "BatchV1Api", "CustomObjectsApi"])))
    def test_concurrent_first_access_builds_one_client(self, name, cls, no_kubeconfig_load):
        """The construction is deliberately slowed so an unlocked cache would lose the race.

        Without the sleep the factory returns fast enough that even a
        non-thread-safe cache usually happens to build only once, and the test
        would pass against a broken implementation.
        """
        builds = []

        def slow_build():
            time.sleep(0.05)
            builds.append(1)
            return MagicMock()

        with patch(f"kubernetes.client.{cls}", side_effect=slow_build):
            results, errors = _hammer(lambda: getattr(kube, name))

        assert not errors
        assert len(results) == 8
        assert len(builds) == 1, f"client was constructed {len(builds)} times"
        assert len({id(r) for r in results}) == 1, "threads received different clients"

    def test_concurrent_new_watch_is_safe_and_uncached(self, no_kubeconfig_load):
        """watched_state spawns a watch thread per resource kind, each calling new_watch()."""
        results, errors = _hammer(kube.new_watch)

        assert not errors
        assert len({id(r) for r in results}) == 8, "watches must not be shared between threads"

    def test_concurrent_first_access_loads_kubeconfig_once(self):
        start = threading.Barrier(8)

        def grab():
            start.wait()
            kube.core_v1

        with patch("kubernetes.config.load_kube_config") as mock_load:
            threads = [threading.Thread(target=grab) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert mock_load.call_count == 1

    def test_lock_is_reentrant(self):
        """The accessors call load_kubeconfig() while already holding the lock."""
        with k8s_api._lock:
            with k8s_api._lock:
                pass


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


class TestNamespace:
    def test_reads_active_context(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "team-ml"}}),
        ):
            assert kube.namespace == "team-ml"

    def test_falls_back_when_context_has_no_namespace(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {}}),
        ):
            assert kube.namespace == "default"

    def test_is_cached(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "team-ml"}}),
        ) as mock_contexts:
            kube.namespace
            kube.namespace
        assert mock_contexts.call_count == 1


class TestNewWatch:
    def test_returns_a_fresh_instance_each_call(self, no_kubeconfig_load):
        """Watches carry per-stream state, so unlike the clients they aren't cached."""
        assert kube.new_watch() is not kube.new_watch()

    def test_is_not_cached_in_instance_dict(self, no_kubeconfig_load):
        kube.new_watch()
        assert "new_watch" not in kube.__dict__


class TestReset:
    def test_clears_clients_namespace_and_kubeconfig(self, no_kubeconfig_load):
        with patch(
            "kubernetes.config.list_kube_config_contexts",
            return_value=([], {"context": {"namespace": "ns"}}),
        ):
            kube.namespace
            for name in CLIENTS:
                getattr(kube, name)

            kube.reset()

            assert kube.__dict__ == {}
            assert k8s_api.load_kubeconfig.cache_info().currsize == 0

    def test_clears_an_injected_fake(self, no_kubeconfig_load):
        """Tests inject by writing __dict__, so reset() must undo that too."""
        kube.__dict__["core_v1"] = "FAKE"
        kube.reset()
        assert kube.core_v1 != "FAKE"


class TestErrorWrapping:
    def test_config_exception_becomes_actionable_runtime_error(self):
        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("boom"),
        ):
            with pytest.raises(RuntimeError, match="KUBECONFIG"):
                kube.batch_v1

    def test_failed_load_is_not_cached_as_success(self):
        """A failed load must keep raising, not leave a half-built client behind."""
        import kubernetes

        with patch(
            "kubernetes.config.load_kube_config",
            side_effect=kubernetes.config.ConfigException("boom"),
        ):
            with pytest.raises(RuntimeError):
                kube.core_v1
            with pytest.raises(RuntimeError):
                kube.core_v1
        assert "core_v1" not in kube.__dict__


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
    def test_k8s_workflow_builds_all_clients_from_kube(self):
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

        with patch("seekr_chain.backends.k8s.k8s_workflow.kube") as mock_kube:
            mock_kube.custom_objects.get_namespaced_custom_object.return_value = {
                "metadata": {"annotations": {"seekr-chain/datastore-root": "s3://bucket/"}}
            }
            mock_kube.namespace = "argo"
            workflow = K8sWorkflow(id="wf-1")

        assert workflow._k8s_v1 is mock_kube.core_v1
        assert workflow._k8s_custom is mock_kube.custom_objects
        assert workflow._namespace == "argo"

    def test_explicit_namespace_skips_the_kubeconfig_lookup(self):
        from seekr_chain.backends.k8s.k8s_workflow import K8sWorkflow

        with patch("seekr_chain.backends.k8s.k8s_workflow.kube") as mock_kube:
            mock_kube.custom_objects.get_namespaced_custom_object.return_value = {
                "metadata": {"annotations": {"seekr-chain/datastore-root": "s3://bucket/"}}
            }
            mock_kube.namespace = "should-not-be-used"
            workflow = K8sWorkflow(id="wf-1", namespace="explicit")

        assert workflow._namespace == "explicit"
