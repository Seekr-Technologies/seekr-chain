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
import time
from typing import Callable, Optional

from seekr_chain.backends.k8s.workflow_state import WorkflowState

logger = logging.getLogger(__name__)


class ContinuousFetchError(RuntimeError):
    """Raised when fetch failures persist continuously past the error tolerance.

    Carries the unique exceptions seen during the failed streak so the caller
    can report what went wrong. The follow session exits when this is raised.
    """

    def __init__(self, elapsed: float, errors: list[Exception]):
        self.elapsed = elapsed
        self.errors = errors
        summary = "; ".join(f"{type(e).__name__}: {e}" for e in errors)
        super().__init__(
            f"state fetch failed continuously for {elapsed:.1f}s "
            f"({len(errors)} unique error(s): {summary})"
        )


class BackgroundStateFetcher:
    """Repeatedly call ``fetch_fn`` on a daemon thread; publish the latest result.

    Exceptions from ``fetch_fn`` are swallowed so a single API blip does not
    tear down the follow session — the last good snapshot keeps being served
    via :meth:`latest`.

    Error handling policy:

    * ``transient_check(e)`` returning truthy marks ``e`` as on the "swallow
      list": it is logged at **DEBUG** (silent) rather than WARNING. Use this
      for self-resolving blips such as a 401 during a token-refresh window.
    * Any other exception is logged at **WARNING** on every occurrence.
    * If fetch failures of **any** kind persist continuously for
      ``error_tolerance`` seconds (no successful fetch in between), the fetcher
      records a :class:`ContinuousFetchError` and stops. The error is raised
      from :meth:`latest` (and :meth:`wait_for_first`) on the caller's thread
      so the follow session exits rather than running forever against stale
      state.

    A single successful fetch resets the streak, so an isolated blip that
    recovers never surfaces or escalates.

    Usage::

        with BackgroundStateFetcher(workflow.get_detailed_state) as f:
            state = f.wait_for_first()
            while not state.status.is_finished():
                render(state)
                time.sleep(1)
                state = f.latest()
    """

    def __init__(
        self,
        fetch_fn: Callable[[], WorkflowState],
        interval: float = 1.0,
        transient_check: Optional[Callable[[Exception], bool]] = None,
        error_tolerance: float = 30.0,
    ):
        self._fetch_fn = fetch_fn
        self._interval = interval
        self._transient_check = transient_check
        self._error_tolerance = error_tolerance
        self._lock = threading.Lock()
        self._latest: Optional[WorkflowState] = None
        self._first_ready = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Continuous-error streak state, guarded by _lock.
        # _streak_start == 0.0 means no active streak.
        self._streak_start: float = 0.0
        self._streak_errors: list[Exception] = []
        # Set when the streak exceeds the tolerance; re-raised from latest().
        self._fatal_error: Optional[ContinuousFetchError] = None

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
            if self._fatal_error is not None:
                raise self._fatal_error
            return self._latest

    def wait_for_first(self, timeout: Optional[float] = None) -> WorkflowState:
        """Block until the first successful fetch. Raises ``TimeoutError`` on timeout."""
        if not self._first_ready.wait(timeout=timeout):
            # The fetcher may have died with a fatal error before the first success.
            with self._lock:
                if self._fatal_error is not None:
                    raise self._fatal_error
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
            t0 = time.monotonic()
            try:
                state = self._fetch_fn()
            except Exception as e:
                if self._handle_fetch_error(e):
                    return  # fatal error set — stop the fetcher thread
            else:
                with self._lock:
                    self._latest = state
                    self._streak_start = 0.0
                    self._streak_errors = []
                self._first_ready.set()
            # Aim for one fetch per ``interval`` seconds: if the fetch already
            # took that long, refetch immediately rather than adding idle time.
            remaining = self._interval - (time.monotonic() - t0)
            if remaining > 0:
                self._stop.wait(timeout=remaining)

    def _handle_fetch_error(self, e: Exception) -> bool:
        """Record a fetch failure; log it. Return True if the streak is fatal.

        On a fatal streak (elapsed >= tolerance), sets ``self._fatal_error`` and
        returns True so ``_run`` stops the fetcher thread. The error is re-raised
        from :meth:`latest` on the caller's thread.
        """
        with self._lock:
            now = time.monotonic()
            if self._streak_start == 0.0:
                self._streak_start = now
                self._streak_errors = []
            elapsed = now - self._streak_start
            # Track unique errors by (type, message) so we don't accumulate
            # duplicates over a long streak.
            key = (type(e), str(e))
            if not any((type(x), str(x)) == key for x in self._streak_errors):
                self._streak_errors.append(e)
            errors_snapshot = list(self._streak_errors)

        is_transient = self._transient_check(e) if self._transient_check is not None else False
        if is_transient:
            logger.debug("transient state fetch failed: %s", e)
        else:
            logger.warning("state fetch failed: %s", e)

        if elapsed >= self._error_tolerance:
            fatal = ContinuousFetchError(elapsed, errors_snapshot)
            with self._lock:
                self._fatal_error = fatal
            logger.warning("%s", fatal)
            return True
        return False
