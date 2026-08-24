"""In-memory Fake Kubernetes cluster for testing the controller pod's DAG
executor (controllerlib), replacing hand-built event dicts and MagicMock
call-arg inspection with cluster-state mutations.

Tests express scenarios in terms of what happened to the cluster
(``cluster.complete_jobset("a-js")``) rather than what the controller's k8s
client calls looked like, so the harness survives controller-internals
changes (e.g. future per-attempt JobSet resubmission) without being
rewritten.
"""

import copy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException

# Same bootstrap as test_controller.py — safe to repeat, sys.path.insert is
# idempotent for a given path and fake_k8s.py may be imported on its own.
_RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
sys.path.insert(0, str(_RESOURCES))

from controller import scheduling, watch  # noqa: E402


class _FakeCustomObjectsApi:
    """Stands in for kubernetes.client.CustomObjectsApi()."""

    def __init__(self, cluster: "FakeK8sCluster"):
        self._cluster = cluster

    def get_namespaced_custom_object(self, group, version, plural, namespace, name):
        cluster = self._cluster
        if name not in cluster.jobsets:
            raise ApiException(status=404)
        return cluster.jobsets[name]

    def create_namespaced_custom_object(self, group, version, plural, namespace, body):
        cluster = self._cluster
        cluster.create_attempts += 1

        if cluster._next_create_exception is not None:
            exc = cluster._next_create_exception
            cluster._next_create_exception = None
            raise exc

        name = body["metadata"]["name"]
        if name in cluster.jobsets:
            raise ApiException(status=409)

        stored = {
            "metadata": dict(body.get("metadata", {})),
            "spec": dict(body.get("spec", {})),
            "status": dict(body.get("status", {})),
        }
        stored["metadata"]["resourceVersion"] = cluster._bump_rv()
        cluster.jobsets[name] = stored
        return stored

    def list_namespaced_custom_object(self, group, version, plural, namespace, **kwargs):
        cluster = self._cluster
        return {
            "metadata": {"resourceVersion": str(cluster._resource_version)},
            "items": list(cluster.jobsets.values()),
        }


class _FakeCoreV1Api:
    """Stands in for kubernetes.client.CoreV1Api()."""

    def __init__(self, cluster: "FakeK8sCluster"):
        self._cluster = cluster

    def read_namespaced_config_map(self, name, namespace):
        cluster = self._cluster
        if name not in cluster.configmaps:
            raise ApiException(status=404)
        return MagicMock(data=cluster.configmaps[name]["data"])

    def patch_namespaced_config_map(self, name, namespace, body):
        cluster = self._cluster
        if name not in cluster.configmaps:
            raise ApiException(status=404)
        cluster.configmaps[name]["data"].update(body.get("data", {}))

    def create_namespaced_config_map(self, namespace, body):
        cluster = self._cluster
        name = body["metadata"]["name"]
        cluster.configmaps[name] = {"data": dict(body.get("data", {}))}

    def create_namespaced_event(self, namespace, body):
        self._cluster.events.append(body)


class FakeWatch:
    """Stands in for kubernetes.watch.Watch()."""

    def __init__(self, cluster: "FakeK8sCluster"):
        self.cluster = cluster

    def stream(self, func, **kwargs):
        self.cluster.watch_last_kwargs = kwargs
        if self.cluster._next_stream_exception is not None:
            exc = self.cluster._next_stream_exception
            self.cluster._next_stream_exception = None
            raise exc
        queue, self.cluster._watch_queue = self.cluster._watch_queue, []
        yield from queue

    def stop(self):
        pass


class FakeK8sCluster:
    """An in-memory Kubernetes cluster: JobSets, phases ConfigMaps, and
    Events, plus enough watch-stream/create-call fault injection to drive
    the controller through its reconnect and retry paths."""

    def __init__(self):
        self.jobsets: dict[str, dict] = {}
        self.configmaps: dict[str, dict] = {}
        self.events: list[dict] = []
        self.create_attempts = 0
        self.watch_last_kwargs: dict | None = None
        self.last_watch: FakeWatch | None = None
        self._resource_version = 0
        self._watch_queue: list[dict] = []
        self._next_stream_exception: Exception | None = None
        self._next_create_exception: Exception | None = None

    def _bump_rv(self) -> str:
        self._resource_version += 1
        return str(self._resource_version)

    def _enqueue(self, name: str) -> None:
        self._watch_queue.append({"type": "MODIFIED", "object": copy.deepcopy(self.jobsets[name])})

    def set_controller_jobset(self, name: str, uid: str) -> None:
        """Seed the controller's own JobSet (self-read via
        get_namespaced_custom_object at main() startup)."""
        self.jobsets[name] = {
            "metadata": {"name": name, "uid": uid, "resourceVersion": self._bump_rv()},
            "spec": {"suspend": False},
            "status": {},
        }

    def submit_jobset(self, name: str, *, suspend: bool = False) -> None:
        """Pre-populate an already-existing JobSet (for 409-on-create /
        already-exists-on-restart scenarios). Does NOT enqueue a watch
        event — this models state that existed before the watch opened."""
        self.jobsets[name] = {
            "metadata": {"name": name, "resourceVersion": self._bump_rv()},
            "spec": {"suspend": suspend},
            "status": {},
        }

    def _terminal_jobset(self, name: str) -> dict:
        return self.jobsets.setdefault(
            name,
            {"metadata": {"name": name}, "spec": {"suspend": False}, "status": {}},
        )

    def complete_jobset(self, name: str) -> None:
        """Mark a jobset Completed and enqueue a MODIFIED watch event."""
        js = self._terminal_jobset(name)
        js["status"] = {"terminalState": "Completed"}
        js["metadata"]["resourceVersion"] = self._bump_rv()
        self._enqueue(name)

    def fail_jobset(self, name: str) -> None:
        js = self._terminal_jobset(name)
        js["status"] = {"terminalState": "Failed"}
        js["metadata"]["resourceVersion"] = self._bump_rv()
        self._enqueue(name)

    def cancel_jobset(self, name: str) -> None:
        js = self._terminal_jobset(name)
        js["spec"]["suspend"] = True
        js["metadata"]["resourceVersion"] = self._bump_rv()
        self._enqueue(name)

    def raise_on_next_stream(self, exc: Exception) -> None:
        """The next Watch().stream() call raises `exc` immediately instead
        of yielding queued events."""
        self._next_stream_exception = exc

    def fail_next_create(self, status_code: int) -> None:
        """The next create_namespaced_custom_object call raises
        ApiException(status=status_code) instead of creating."""
        self._next_create_exception = ApiException(status=status_code)

    def custom_objects_api(self) -> _FakeCustomObjectsApi:
        return _FakeCustomObjectsApi(self)

    def core_v1_api(self) -> _FakeCoreV1Api:
        return _FakeCoreV1Api(self)

    def watch(self) -> FakeWatch:
        w = FakeWatch(self)
        self.last_watch = w
        return w


def run_controller_main(
    cluster: FakeK8sCluster,
    dag: list[dict],
    *,
    job_name: str = "wf-abc",
    namespace: str = "ns",
    assets_path: str = "/assets",
):
    """Patch kubernetes.config/client/watch to `cluster`, patch open()/json.load
    for dag.json, set env vars, call controller.watch.main(), return
    (result, cluster)."""
    if job_name not in cluster.jobsets:
        cluster.set_controller_jobset(job_name, "uid-123")

    env = {
        "SEEKR_CHAIN_JOB_ASSET_PATH": assets_path,
        "SEEKR_CHAIN_NAMESPACE": namespace,
        "SEEKR_CHAIN_CONTROLLER_JOB_NAME": job_name,
    }

    def _load_manifest_mock(_assets, name):
        return {"metadata": {"name": f"{name}-js"}, "spec": {}}

    with (
        patch.dict("os.environ", env),
        patch.object(watch.kubernetes.config, "load_incluster_config"),
        patch.object(watch.kubernetes.client, "CustomObjectsApi", cluster.custom_objects_api),
        patch.object(watch.kubernetes.client, "CoreV1Api", cluster.core_v1_api),
        patch.object(watch.kubernetes.watch, "Watch", cluster.watch),
        patch.object(scheduling, "load_manifest", side_effect=_load_manifest_mock),
        patch.object(watch.time, "sleep"),
        patch(
            "builtins.open",
            MagicMock(
                return_value=MagicMock(
                    __enter__=lambda s, *a: s,
                    __exit__=lambda s, *a: None,
                    read=MagicMock(return_value=""),
                )
            ),
        ),
        patch.object(watch.json, "load", return_value=dag),
    ):
        result = watch.main()

    return result, cluster
