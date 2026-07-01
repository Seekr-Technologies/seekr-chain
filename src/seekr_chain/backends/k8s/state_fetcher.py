#!/usr/bin/env python3
"""
Background fetcher for ``WorkflowState`` snapshots.

The k8s status fetch (:func:`get_workflow_state`) issues three sequential
API calls and can take 1-3 s under load. Running it inline in a display
loop makes timers stutter. :class:`BackgroundStateFetcher` runs the fetch
on a daemon thread so the display loop can re-render the last-known
state every second while a fresh fetch is in flight — relative durations
in the renderer already tick against ``datetime.now()``.
"""

import logging
import threading
from typing import Callable, Optional

from seekr_chain.backends.k8s.workflow_state import WorkflowState

logger = logging.getLogger(__name__)


class BackgroundStateFetcher:
    """Repeatedly call ``fetch_fn`` on a daemon thread; publish the latest result.

    Transient exceptions from ``fetch_fn`` are logged and swallowed so a
    single API blip does not tear down the follow session — the last good
    snapshot keeps being served via :meth:`latest`.

    Usage::

        with BackgroundStateFetcher(workflow.get_detailed_state) as f:
            state = f.wait_for_first()
            while not state.status.is_finished():
                render(state)
                time.sleep(1)
                state = f.latest()
    """

    def __init__(self, fetch_fn: Callable[[], WorkflowState], interval: float = 1.0):
        self._fetch_fn = fetch_fn
        self._interval = interval
        self._lock = threading.Lock()
        self._latest: Optional[WorkflowState] = None
        self._first_ready = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="workflow-state-fetcher")
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

    def latest(self) -> Optional[WorkflowState]:
        with self._lock:
            return self._latest

    def wait_for_first(self, timeout: Optional[float] = None) -> WorkflowState:
        """Block until the first successful fetch. Raises ``TimeoutError`` on timeout."""
        if not self._first_ready.wait(timeout=timeout):
            raise TimeoutError("Timed out waiting for first workflow state fetch")
        state = self.latest()
        assert state is not None  # _first_ready is only set after a successful publish
        return state

    def __enter__(self) -> "BackgroundStateFetcher":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                state = self._fetch_fn()
            except Exception as e:
                # Swallow so a transient K8s API blip doesn't kill the loop.
                logger.warning("state fetch failed: %s", e)
            else:
                with self._lock:
                    self._latest = state
                self._first_ready.set()
            # Event.wait returns True immediately if set — cheap responsive stop.
            self._stop.wait(timeout=self._interval)
