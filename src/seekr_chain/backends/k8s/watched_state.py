#!/usr/bin/env python3
"""
Watch-driven state builders — replace per-second polling with Kubernetes
Watch API streams feeding a shared in-memory cache.

``ReconnectingWatcher`` watches one or more Kubernetes resource kinds (each
described by a ``WatchSpec``) and projects their caches into a snapshot via a
``project`` callback. Two factory functions configure it for the two use
cases in this codebase: ``workflow_state_watcher()`` watches the controller
Job *and* JobSets *and* Pods to build a full per-step/pod ``WorkflowState``
for ``K8sWorkflow.follow()``/``attach()``; ``controller_status_watcher()``
watches only the controller Job to back ``K8sWorkflow.watch_controller_status()``
(used by ``wait()``, which may be watching many jobs concurrently and only
needs the overall status).

Uses the standard Kubernetes "list-then-watch" pattern: one synchronous
fetch to seed the cache and capture each resource's resourceVersion, then
a daemon thread per resource kind that watches from that resourceVersion
onward, applying ADDED/MODIFIED/DELETED events to the cache. Reconnects
on transient errors and re-lists from scratch on 410 Gone (resourceVersion
too old) — mirrors ``resources/controller.py``.
"""

import functools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from kubernetes.client.rest import ApiException
from rich.console import Console

from seekr_chain.backends.k8s.workflow_state import (
    WorkflowState,
    build_workflow_state,
    job_status_and_completion,
    list_jobsets,
    list_pods,
    read_workflow_job,
)
from seekr_chain.k8s_api import kube
from seekr_chain.status import WorkflowStatus

logger = logging.getLogger(__name__)

_WATCH_RECONNECT_DELAY = 2  # server-side timeout_seconds for a healthy stream's periodic reconnect
# Client-side (connect, read) socket timeout. ``timeout_seconds`` above only
# asks the API server to end the stream after a quiet period — it does
# nothing if the connection dies at the network layer (e.g. VPN drop), since
# the server never gets a chance to respond. Without this, a dead socket
# blocks forever with no exception raised, so failures never get recorded or
# escalated. The read timeout resets on every chunk received, so it doesn't
# interrupt a healthy, actively-streaming watch.
# Kept tight since a dead connection is now handled gracefully by the backoff
# below — there's no upside to waiting longer to detect one. Read is a few
# seconds above ``_WATCH_RECONNECT_DELAY`` (the server's own idle-stream
# timeout) so a normal server-initiated close isn't mistaken for a hang.
_WATCH_REQUEST_TIMEOUT = (3, 7)  # (connect, read) seconds

# Retry backoff on failure: delay doubles each consecutive attempt, capped at
# _WATCH_BACKOFF_MAX_SECONDS, until _WATCH_MAX_ATTEMPTS is reached and we give up.
#
# Worst-case time to escalate isn't just the backoff sum — every retry after
# the first also has to re-establish the connection, which can itself block
# for up to the connect timeout above before the backoff delay even starts.
# With max_attempts=5: 7s (first attempt's read timeout) + 1+2+4+8 (backoff
# between the 5 attempts) + 4*3s (connect timeout on each retry) = ~34s.
_WATCH_BACKOFF_BASE_SECONDS = 1
_WATCH_BACKOFF_MAX_SECONDS = 30
_WATCH_MAX_ATTEMPTS = 5


class WatchStalledError(RuntimeError):
    """Raised when a watch has failed on every attempt up to the retry limit.

    The full ``str(e)`` (used for logging) includes the deduplicated set of
    underlying errors seen, which can be long. ``kind``/``attempts``/
    ``elapsed_seconds``/``job_id`` are exposed separately so callers can show
    a short summary (see ``print_reconnect_hint``) instead of parsing that
    string.
    """

    def __init__(self, message: str, *, kind: str, attempts: int, elapsed_seconds: float, job_id: str):
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.job_id = job_id


@dataclass
class WatchDisconnection:
    """A currently-ongoing watch failure streak, for display while retrying."""

    kind: str  # "job" | "jobsets" | "pods"
    attempt: int
    max_attempts: int
    elapsed_seconds: float
    retry_in_seconds: float  # counts down to 0; 0 means the retry attempt is in progress now


def _backoff_delay(attempt: int) -> float:
    return min(_WATCH_BACKOFF_MAX_SECONDS, _WATCH_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def _format_watch_error(e: Exception) -> str:
    """Reduce an exception to a short, dedup-friendly summary.

    urllib3's MaxRetryError.__str__() embeds a connection object's repr
    (e.g. "<urllib3.connection.HTTPSConnection object at 0x...>"), whose
    address differs on every retry. Left alone, that defeats _record_failure's
    set()-based dedup and produces a wall of near-identical text for what is
    really one repeated failure. Unwrapping to the innermost .reason/.args
    gives a message that's stable across retries and actually dedups.
    """
    if isinstance(e, ApiException):
        return f"{e.status} {e.reason}".strip()
    reason = getattr(e, "reason", e)
    args = getattr(reason, "args", None)
    if args:
        return f"{type(reason).__name__}: {args[-1]}"
    return f"{type(e).__name__}: {e}"


def print_reconnect_hint(e: "WatchStalledError") -> None:
    """Print a short, colored explanation of a WatchStalledError plus a
    reconnect hint, instead of leaving its traceback as the only signal.

    Callers (follow()/attach()/watch_controller_status() in k8s_workflow.py)
    must call this only after any rich.Live display has stopped — Live owns
    the terminal while active, so printing to a separate Console while it's
    still rendering produces interleaved, mangled output. This is why the
    print doesn't happen where the error is detected (inside the background
    watch thread, mid-render) and instead happens once the watch's ``with``
    block has exited, right before re-raising.
    """
    console = Console(stderr=True)
    console.print(
        f"[bold red]Disconnected:[/bold red] lost connection to the cluster "
        f"({e.kind} watch failed after {e.attempts} attempts over {e.elapsed_seconds:.0f}s)."
    )
    console.print(
        "[yellow]This is likely due to a network/VPN error. Fix your connection and reconnect with:[/yellow]\n"
    )
    console.print(f"  chain logs -f {e.job_id}\n")


@dataclass
class WatchSpec:
    """Declarative description of one watched Kubernetes resource kind."""

    kind: str  # "job" | "jobsets" | "pods" — also the cache key and failure-tracking key
    seed: Callable[[], tuple[list, str]]  # synchronous list/read -> (objects, resourceVersion)
    list_fn: Callable  # the streaming watch call, e.g. k8s_batch.list_namespaced_job
    list_kwargs: dict  # selector kwargs for the stream (NOT timeout/resource_version — added per-attempt)
    key: Callable[[object], str]  # event object -> cache key (name)
    rv: Callable[[object], object]  # event object -> resourceVersion (falsy -> ignored)


def _v1_name(obj) -> str:
    return obj.metadata.name


def _v1_rv(obj):
    return obj.metadata.resource_version


def _dict_name(obj) -> str:
    return obj["metadata"]["name"]


def _dict_rv(obj):
    return obj.get("metadata", {}).get("resourceVersion")


def _seed_controller_job(k8s_batch, namespace, workflow_id) -> tuple[list, str]:
    """Seed the job cache from a single read, not a list.

    Presented as a 0-or-1-element list so the controller Job is driven by the
    same generic list-then-watch machinery as JobSets/Pods, without changing
    the underlying ``read_workflow_job()`` 404-means-gone semantics that
    ``watch_controller_status()`` depends on.
    """
    job = read_workflow_job(k8s_batch, namespace, workflow_id)
    if job is None:
        return [], ""
    return [job], job.metadata.resource_version


def _project_workflow_state(workflow_id, caches) -> WorkflowState:
    job = next(iter(caches["job"].values()), None)
    return build_workflow_state(workflow_id, job, list(caches["jobsets"].values()), list(caches["pods"].values()))


def _project_controller_status(caches) -> Optional[WorkflowStatus]:
    job = next(iter(caches["job"].values()), None)
    if job is None:
        return None
    return job_status_and_completion(job)[0]


class ReconnectingWatcher:
    """Maintain a live, projected snapshot of one or more Kubernetes resource
    kinds via Watch API streams instead of polling.

    Configured with a list of ``WatchSpec``s (one per resource kind) and a
    ``project`` callback that turns the current per-kind caches into
    whatever snapshot type the caller wants (a full ``WorkflowState``, or
    just a ``WorkflowStatus``) — see ``workflow_state_watcher()`` /
    ``controller_status_watcher()`` below, the intended construction sites.

    Usage::

        with workflow_state_watcher(k8s_custom, k8s_v1, k8s_batch, namespace, workflow_id) as w:
            state = w.wait_for_first()
            while not state.status.is_finished():
                render(state)
                w.wait_for_update(timeout=1.0)
                state = w.latest()
    """

    def __init__(
        self,
        label: str,
        specs: list[WatchSpec],
        project: Callable[[dict], object],
        max_attempts: int = _WATCH_MAX_ATTEMPTS,
    ):
        self._label = label
        self._max_attempts = max_attempts
        self._specs = list(specs)
        self._project = project

        self._lock = threading.Lock()
        self._stop = threading.Event()

        self._fatal_error: Optional[Exception] = None
        self._failure_since: dict[str, float] = {}
        self._failure_errors: dict[str, set] = {}
        self._attempts: dict[str, int] = {}
        self._next_attempt_at: dict[str, float] = {}

        self._caches: dict[str, dict] = {spec.kind: {} for spec in self._specs}
        self._latest = None
        # Set by each watch thread on every event; latest() rebuilds lazily
        # from this instead of every thread rebuilding the whole snapshot
        # under the lock on every single event, which doesn't scale with
        # cache size under bursts of events (e.g. a wide fan-out restarting).
        # Coalesces any events that land between two latest() calls into a
        # single rebuild.
        self._dirty = False

        self._changed = threading.Event()
        self._first_ready = threading.Event()
        self._threads: list[threading.Thread] = []

    def _on_fatal(self) -> None:
        """Called (holding ``self._lock``) the moment a watch becomes fatally stalled."""
        self._first_ready.set()

    def _on_fatal_logged(self) -> None:
        """Called after the fatal-error log line, outside the lock."""
        self._changed.set()

    def _raise_if_fatal(self) -> None:
        with self._lock:
            if self._fatal_error is not None:
                raise self._fatal_error

    def _record_success(self, kind: str) -> None:
        with self._lock:
            self._failure_since.pop(kind, None)
            self._failure_errors.pop(kind, None)
            self._attempts.pop(kind, None)
            self._next_attempt_at.pop(kind, None)

    def _record_failure(self, kind: str, error: Exception) -> float:
        """Track a watch failure; escalate to a fatal error once one kind has
        failed on ``self._max_attempts`` consecutive attempts.

        Individual watch errors stay at debug level (see the call sites) — this
        only surfaces once the retry limit is hit, at which point we log the
        deduplicated set of errors seen and stop all watchers rather than
        waiting for the other kinds (if any) to also exhaust their retries.

        Returns the backoff delay (seconds) the caller should wait before its
        next retry.
        """
        with self._lock:
            if self._fatal_error is not None:
                return 0.0
            since = self._failure_since.setdefault(kind, time.monotonic())
            errors = self._failure_errors.setdefault(kind, set())
            errors.add(_format_watch_error(error))
            attempt = self._attempts[kind] = self._attempts.get(kind, 0) + 1
            elapsed = time.monotonic() - since
            should_escalate = attempt >= self._max_attempts
            if should_escalate:
                message = (
                    f"{kind} watch failed after {attempt} attempts over {elapsed:.0f}s: {'; '.join(sorted(errors))}"
                )
                self._fatal_error = WatchStalledError(
                    message, kind=kind, attempts=attempt, elapsed_seconds=elapsed, job_id=self._label
                )
                self._stop.set()
                self._on_fatal()

        delay = _backoff_delay(attempt)
        with self._lock:
            self._next_attempt_at[kind] = time.monotonic() + delay

        if should_escalate:
            logger.error("Giving up on watched state for %s: %s", self._label, message)
            self._on_fatal_logged()

        return delay

    def _call_with_retry(self, kind: str, fn):
        """Call ``fn()``, retrying with the same backoff/attempt-limit
        machinery as the watch threads on failure.

        The initial synchronous seed read in ``start()`` (list/read calls
        made in ``__enter__``, before any watch thread exists) previously
        raised straight out of ``__enter__`` on a transient error — exactly
        the VPN-drop class this reconnect machinery exists to handle — with
        no backoff and no ``WatchStalledError``, bypassing the graceful
        handling entirely. Routing it through ``_record_failure`` means a
        blip here counts against the same attempt streak as watch failures.
        """
        while True:
            try:
                result = fn()
                self._record_success(kind)
                return result
            except Exception as e:
                delay = self._record_failure(kind, e)
                with self._lock:
                    fatal = self._fatal_error
                if fatal is not None:
                    raise fatal
                time.sleep(delay)

    def connection_status(self) -> Optional[WatchDisconnection]:
        """Return the currently-longest-running watch failure streak, if any.

        Reflects retries in progress — nothing to do with ``WatchStalledError``,
        which is only raised once a streak exhausts its retry budget. Callers
        (``follow()``/``attach()``) poll this alongside ``latest()`` to show a
        "reconnecting" banner while a watch is down but not yet fatal.
        """
        with self._lock:
            if not self._failure_since:
                return None
            kind = min(self._failure_since, key=lambda k: self._failure_since[k])
            since = self._failure_since[kind]
            attempt = self._attempts.get(kind, 0)
            next_attempt_at = self._next_attempt_at.get(kind, 0.0)
        return WatchDisconnection(
            kind=kind,
            attempt=attempt,
            max_attempts=self._max_attempts,
            elapsed_seconds=time.monotonic() - since,
            retry_in_seconds=max(0.0, next_attempt_at - time.monotonic()),
        )

    def start(self) -> None:
        if self._threads:
            return

        resource_versions = {}
        for spec in self._specs:
            items, rv = self._call_with_retry(spec.kind, spec.seed)
            with self._lock:
                self._caches[spec.kind] = {spec.key(o): o for o in items}
            resource_versions[spec.kind] = rv

        with self._lock:
            self._rebuild_locked()

        self._threads = [
            threading.Thread(
                target=self._run_watch,
                args=(spec, resource_versions[spec.kind]),
                daemon=True,
                name=f"watch-{spec.kind}",
            )
            for spec in self._specs
        ]
        for t in self._threads:
            t.start()
        self._first_ready.set()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=join_timeout)

    def __enter__(self) -> "ReconnectingWatcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def latest(self):
        with self._lock:
            if self._dirty:
                self._rebuild_locked()
                self._dirty = False
            return self._latest

    def wait_for_first(self, timeout: Optional[float] = None):
        """Block until the first snapshot is available. Raises ``TimeoutError`` on timeout."""
        if not self._first_ready.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for first watch snapshot")
        self._raise_if_fatal()
        return self.latest()

    def wait_for_update(self, timeout: Optional[float] = None) -> bool:
        """Block until a cache changes or timeout elapses. Returns whether it changed.

        Raises ``WatchStalledError`` if a watch has failed on every attempt up to
        the retry limit.
        """
        triggered = self._changed.wait(timeout=timeout)
        if triggered:
            self._changed.clear()
        self._raise_if_fatal()
        return triggered

    def _rebuild_locked(self) -> None:
        """Rebuild ``self._latest`` from the caches. Caller must hold ``self._lock``."""
        self._latest = self._project(self._caches)

    def _apply_event(self, spec: WatchSpec, event) -> object:
        obj = event["object"]
        name = spec.key(obj)
        with self._lock:
            cache = self._caches[spec.kind]
            if event["type"] == "DELETED":
                cache.pop(name, None)
            else:
                cache[name] = obj
            self._dirty = True
        self._changed.set()
        return spec.rv(obj)

    def _run_watch(self, spec: WatchSpec, resource_version) -> None:
        """Drive one reconnect-with-backoff watch loop, shared by every watch thread."""
        while not self._stop.is_set():
            try:
                w = kube.new_watch()
                kwargs = dict(spec.list_kwargs)
                kwargs["timeout_seconds"] = _WATCH_RECONNECT_DELAY
                kwargs["_request_timeout"] = _WATCH_REQUEST_TIMEOUT
                if resource_version:
                    kwargs["resource_version"] = resource_version
                for event in w.stream(spec.list_fn, **kwargs):
                    if self._stop.is_set():
                        w.stop()
                        break
                    rv = self._apply_event(spec, event)
                    if rv:
                        resource_version = rv
                self._record_success(spec.kind)
            except ApiException as e:
                if e.status == 410:
                    # Routine "resourceVersion too old, re-list" signal, not a
                    # fault — recording it as a failure would flash a spurious
                    # "Disconnected..." banner and push the watch toward a
                    # premature WatchStalledError. Re-list from scratch, no backoff.
                    logger.debug("%s watch resourceVersion too old (410); re-listing from scratch", spec.kind)
                    resource_version = ""
                    continue
                delay = self._record_failure(spec.kind, e)
                self._stop.wait(delay)
            except Exception as e:
                logger.debug("%s watch error, reconnecting", spec.kind, exc_info=True)
                delay = self._record_failure(spec.kind, e)
                self._stop.wait(delay)


def _job_spec(k8s_batch, namespace, workflow_id) -> WatchSpec:
    return WatchSpec(
        kind="job",
        seed=functools.partial(_seed_controller_job, k8s_batch, namespace, workflow_id),
        list_fn=k8s_batch.list_namespaced_job,
        list_kwargs=dict(namespace=namespace, field_selector=f"metadata.name={workflow_id}"),
        key=_v1_name,
        rv=_v1_rv,
    )


def workflow_state_watcher(
    k8s_custom,
    k8s_v1,
    k8s_batch,
    namespace: str,
    workflow_id: str,
    max_attempts: int = _WATCH_MAX_ATTEMPTS,
) -> ReconnectingWatcher:
    """Build a ``ReconnectingWatcher`` that maintains a live ``WorkflowState``
    for ``K8sWorkflow.follow()``/``attach()`` by watching the controller Job,
    JobSets, and worker Pods.
    """
    specs = [
        _job_spec(k8s_batch, namespace, workflow_id),
        WatchSpec(
            kind="jobsets",
            seed=functools.partial(list_jobsets, k8s_custom, namespace, workflow_id),
            list_fn=k8s_custom.list_namespaced_custom_object,
            list_kwargs=dict(
                group="jobset.x-k8s.io",
                version="v1alpha2",
                plural="jobsets",
                namespace=namespace,
                label_selector=f"seekr-chain/job-id={workflow_id}",
            ),
            key=_dict_name,
            rv=_dict_rv,
        ),
        WatchSpec(
            kind="pods",
            seed=functools.partial(list_pods, k8s_v1, namespace, workflow_id),
            list_fn=k8s_v1.list_namespaced_pod,
            list_kwargs=dict(
                namespace=namespace,
                label_selector=f"seekr-chain/job-id={workflow_id},seekr-chain/is-controller!=true",
            ),
            key=_v1_name,
            rv=_v1_rv,
        ),
    ]
    return ReconnectingWatcher(
        label=workflow_id,
        specs=specs,
        project=functools.partial(_project_workflow_state, workflow_id),
        max_attempts=max_attempts,
    )


def controller_status_watcher(
    k8s_batch,
    namespace: str,
    workflow_id: str,
    max_attempts: int = _WATCH_MAX_ATTEMPTS,
) -> ReconnectingWatcher:
    """Build a ``ReconnectingWatcher`` that maintains a live ``WorkflowStatus``
    for ``K8sWorkflow.watch_controller_status()`` by watching only the
    controller Job.

    Deliberately lighter than ``workflow_state_watcher()``:
    ``watch_controller_status()`` (used by ``wait()``, which may be watching
    many jobs concurrently) only needs the controller Job's own status, not a
    full per-step/pod ``WorkflowState`` — one watch here instead of three
    keeps that cheap.
    """
    return ReconnectingWatcher(
        label=workflow_id,
        specs=[_job_spec(k8s_batch, namespace, workflow_id)],
        project=_project_controller_status,
        max_attempts=max_attempts,
    )
