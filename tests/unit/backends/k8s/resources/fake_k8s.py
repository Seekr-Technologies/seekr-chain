"""In-memory Fake Kubernetes cluster for testing the controller pod's DAG
executor (controllerlib).

Tests script outcomes up front — ``cluster.script_step("a", exit_code=0)`` —
then run the controller once and assert on the resulting trace
(``cluster.trace``), rather than interleaving manual cluster mutations
between watch-loop iterations. A JobSet's outcome is resolved the moment the
controller creates it (via its ``seekr-chain/step-name`` label), so the fake
never needs to be told "now let it finish" — it already knows.

Manual, out-of-band mutation (``complete_jobset``, ``fail_jobset``,
``cancel_jobset``, ``submit_jobset``, ``raise_on_next_stream``,
``fail_next_create``) is still available for scenarios script_step can't
express: state that exists before the controller ever creates the JobSet
(restart-resume), or events not triggered by JobSet creation at all
(transient watch-stream errors).
"""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from kubernetes.client.exceptions import ApiException

# Same bootstrap as test_controller.py — safe to repeat, sys.path.insert is
# idempotent for a given path and fake_k8s.py may be imported on its own.
_RESOURCES = Path(__file__).resolve().parents[5] / "src/seekr_chain/backends/k8s/resources"
sys.path.insert(0, str(_RESOURCES))

# Label the controller stamps on every JobSet manifest (see
# templates/jobset.yaml.j2) — used to map a create call back to the step it
# submits, so a scripted outcome can be resolved without the fake needing to
# know the controller's JobSet naming/attempt scheme.
_STEP_LABEL = "seekr-chain/step-name"

# Label the controller stamps on every pod (see templates/jobset.yaml.j2),
# read back by list_namespaced_pod filtering below.
_ROLE_LABEL = "seekr-chain/role"

# Naming convention shared by _load_manifest_mock (test_controller.py) and
# the manual JobSet helpers below: every JobSet is named "{step}-js". Manual
# helpers never see the controller's step-name label (they model state that
# exists before or outside of a controller create call), so a JobSet name is
# mapped back to its step by stripping this suffix.
_JOBSET_SUFFIX = "-js"


def _decode_configmap_data(data: dict) -> dict:
    """Best-effort JSON-decode a ConfigMap data dict's values — phases/
    timings are both persisted as JSON strings."""
    decoded = {}
    for key, value in data.items():
        try:
            decoded[key] = json.loads(value)
        except (TypeError, ValueError):
            decoded[key] = value
    return decoded


def _parse_label_selector(label_selector: str) -> dict[str, str]:
    """Parse a comma-separated ``k=v`` label selector into a dict. Only
    equality clauses are supported — the only form the controller emits."""
    return dict(pair.split("=", 1) for pair in label_selector.split(",") if "=" in pair)


def _fake_pod(role: str, exit_code: int | None) -> SimpleNamespace:
    """Build a minimal stand-in for a kubernetes.client.V1Pod, shaped exactly
    as controller.failure._collect_pod_failures reads it."""
    terminated = SimpleNamespace(exit_code=exit_code) if exit_code is not None else None
    container_status = SimpleNamespace(name="main", state=SimpleNamespace(terminated=terminated))
    return SimpleNamespace(
        metadata=SimpleNamespace(labels={_ROLE_LABEL: role}),
        status=SimpleNamespace(container_statuses=[container_status]),
    )


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

        step = body.get("metadata", {}).get("labels", {}).get(_STEP_LABEL)

        stored = {
            "metadata": dict(body.get("metadata", {})),
            "spec": dict(body.get("spec", {})),
            "status": dict(body.get("status", {})),
        }
        stored["metadata"]["resourceVersion"] = cluster._bump_rv()
        cluster.jobsets[name] = stored

        if step is not None:
            cluster._js_to_step[name] = step
            cluster.trace.append(("submit", step))
            cluster._resolve_scripted_step(name, step)

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
        data = body.get("data", {})
        cluster.configmaps[name]["data"].update(data)
        cluster._record_phases(data)

    def create_namespaced_config_map(self, namespace, body):
        cluster = self._cluster
        name = body["metadata"]["name"]
        data = dict(body.get("data", {}))
        cluster.configmaps[name] = {"data": data}
        cluster._record_phases(data)

    def create_namespaced_event(self, namespace, body):
        self._cluster.trace.append(("event", body["reason"], body["message"]))

    def list_namespaced_pod(self, namespace, label_selector: str = "", **kwargs):
        cluster = self._cluster
        step = _parse_label_selector(label_selector).get("seekr-chain/step")
        pods = [_fake_pod(p["role"], p.get("exit_code")) for p in cluster.pods.get(step, [])]
        return SimpleNamespace(items=pods)


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
        for item in queue:
            # Record exit/cancel at the moment the controller actually
            # observes the event (yield time), not when the fake resolved or
            # enqueued it — this is what preserves the real interleaving of
            # submits vs. exits (e.g. both branches of a diamond submitted
            # before either exits).
            self.cluster._record_yield(item)
            yield item

    def stop(self):
        pass


class FakeK8sCluster:
    """An in-memory Kubernetes cluster: JobSets, phases ConfigMaps, Events,
    and pods, plus enough watch-stream/create-call fault injection to drive
    the controller through its reconnect and retry paths.

    Setup is spec-then-record: script each step's outcome up front with
    ``script_step``, run the controller once, then assert on ``trace`` — the
    single ordered, append-only record of every side effect the controller
    performed, in the exact order it performed them.
    """

    def __init__(self):
        self.jobsets: dict[str, dict] = {}
        self.configmaps: dict[str, dict] = {}
        self.trace: list[tuple] = []
        self.pods: dict[str, list[dict]] = {}
        self.create_attempts = 0
        self.watch_last_kwargs: dict | None = None
        self.last_watch: FakeWatch | None = None
        self._resource_version = 0
        self._watch_queue: list[dict] = []
        self._next_stream_exception: Exception | None = None
        self._next_create_exception: Exception | None = None
        self._scripts: dict[str, dict] = {}
        self._js_to_step: dict[str, str] = {}
        # Last-recorded phases/status, so idempotent re-writes (the watch
        # loop re-saves and re-derives on every iteration, even when nothing
        # changed) collapse to a single trace entry per real transition.
        self._last_phases: dict | None = None
        self._last_status: str | None = None

    def _bump_rv(self) -> str:
        self._resource_version += 1
        return str(self._resource_version)

    def _enqueue(self, name: str) -> None:
        self._watch_queue.append({"type": "MODIFIED", "object": copy.deepcopy(self.jobsets[name])})

    def _step_for_jobset(self, js_name: str) -> str:
        if js_name in self._js_to_step:
            return self._js_to_step[js_name]
        return js_name.removesuffix(_JOBSET_SUFFIX)

    def _record_phases(self, data: dict) -> None:
        """Append a ``("phases", ...)`` trace entry from a ConfigMap write,
        deduped against the last-recorded phases — the watch loop re-saves
        on every iteration even when nothing changed, and timestamps are
        excluded so those re-saves would otherwise be indistinguishable
        duplicates. Timings are excluded outright — they're non-deterministic
        and would break trace equality."""
        phases = _decode_configmap_data(data).get("phases")
        if phases is not None and phases != self._last_phases:
            self.trace.append(("phases", phases))
            self._last_phases = phases

    def record_status(self, status: str) -> None:
        """Append a ``("status", ...)`` trace entry, deduped against the
        last-recorded status for the same reason _record_phases dedupes."""
        if status != self._last_status:
            self.trace.append(("status", status))
            self._last_status = status

    def _record_yield(self, item: dict) -> None:
        """Append an ``("exit", step, code)`` or ``("cancel", step)`` trace
        entry for a watch event as it is yielded to the controller."""
        obj = item.get("object", {})
        js_name = obj.get("metadata", {}).get("name")
        if not js_name:
            return
        step = self._step_for_jobset(js_name)
        terminal = obj.get("status", {}).get("terminalState")
        suspended = obj.get("spec", {}).get("suspend", False)
        if terminal == "Completed":
            self.trace.append(("exit", step, 0))
        elif terminal == "Failed":
            code = self._scripts.get(step, {}).get("exit_code", 1)
            self.trace.append(("exit", step, code))
        elif suspended:
            self.trace.append(("cancel", step))

    def script_step(
        self,
        step: str,
        *,
        exit_code: int = 0,
        pods: list[dict] | None = None,
        cancel: bool = False,
    ) -> None:
        """Script what happens when the controller submits `step`'s JobSet:
        the fake resolves it the instant it's created, enqueuing the matching
        watch event immediately — no separate complete_jobset/fail_jobset/
        cancel_jobset call needed.

        `cancel=True` scripts an externally-triggered cancellation (JobSet
        suspended, no terminalState) instead of a normal exit.

        `pods` (a list of {"role": str, "exit_code": int | None} dicts), if
        given, becomes visible via list_namespaced_pod under this step's
        label selector, for testing the pod-listing retry-decision path.

        A step with no script defaults to succeeding immediately.
        """
        self._scripts[step] = {"exit_code": exit_code, "pods": pods or [], "cancel": cancel}

    def _resolve_scripted_step(self, js_name: str, step: str) -> None:
        outcome = self._scripts.get(step, {"exit_code": 0, "pods": [], "cancel": False})
        js = self.jobsets[js_name]
        if outcome.get("cancel"):
            js["spec"]["suspend"] = True
        else:
            js["status"] = {"terminalState": "Completed" if outcome["exit_code"] == 0 else "Failed"}
        js["metadata"]["resourceVersion"] = self._bump_rv()
        self._enqueue(js_name)
        if outcome["pods"]:
            self.pods[step] = outcome["pods"]

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
        """Mark a jobset Completed and enqueue a MODIFIED watch event.

        Out-of-band: for state that must exist before the controller submits
        the JobSet itself (e.g. modeling a pre-existing JobSet on restart).
        For a JobSet the controller submits normally, prefer script_step.
        """
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
        """Suspend a jobset (models an external `chain cancel`) and enqueue a
        MODIFIED watch event. Not expressible via script_step — cancellation
        isn't an outcome of the controller's own create call."""
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
