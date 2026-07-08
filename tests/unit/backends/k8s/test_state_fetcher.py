"""
Unit tests for BackgroundStateFetcher.

Uses SimpleNamespace as a stand-in for WorkflowState — the fetcher is
generic over its fetch_fn's return type, so the tests never build a real
WorkflowState.
"""

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from seekr_chain.backends.k8s.state_fetcher import BackgroundStateFetcher, ContinuousFetchError


def test_latest_none_before_first_fetch():
    # Block fetch_fn until we say so; latest() should return None until then.
    gate = threading.Event()

    def fetch_fn():
        gate.wait(timeout=1)
        return SimpleNamespace(tag="first")

    with BackgroundStateFetcher(fetch_fn, interval=0.01) as f:
        assert f.latest() is None
        gate.set()
        state = f.wait_for_first(timeout=1)
        assert state.tag == "first"
        assert f.latest().tag == "first"


def test_latest_reflects_most_recent_result():
    counter = {"n": 0}
    lock = threading.Lock()

    def fetch_fn():
        with lock:
            counter["n"] += 1
            return SimpleNamespace(tag=f"v{counter['n']}")

    with BackgroundStateFetcher(fetch_fn, interval=0.01) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if counter["n"] >= 3:
                break
            time.sleep(0.01)
        assert counter["n"] >= 3
        assert f.latest().tag != "v1"


def test_wait_for_first_times_out_when_fetch_hangs():
    hang = threading.Event()

    def fetch_fn():
        hang.wait()  # never returns during the test
        return SimpleNamespace(tag="never")

    with BackgroundStateFetcher(fetch_fn, interval=0.01) as f:
        with pytest.raises(TimeoutError):
            f.wait_for_first(timeout=0.05)
        hang.set()


def test_exception_in_fetch_fn_is_swallowed_and_last_good_state_survives(caplog):
    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(tag="good")
        raise RuntimeError("boom")

    with BackgroundStateFetcher(fetch_fn, interval=0.01, error_tolerance=10.0) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and calls["n"] < 4:
            time.sleep(0.01)
        assert calls["n"] >= 4
        assert f.latest().tag == "good"

    assert any("state fetch failed" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_stop_is_idempotent():
    def fetch_fn():
        return SimpleNamespace(tag="v")

    f = BackgroundStateFetcher(fetch_fn, interval=0.01)
    f.start()
    f.wait_for_first(timeout=1)
    f.stop()
    f.stop()
    assert not f._thread.is_alive()


def test_context_manager_cleans_up_on_exception():
    def fetch_fn():
        return SimpleNamespace(tag="v")

    fetcher = BackgroundStateFetcher(fetch_fn, interval=0.01)
    with pytest.raises(ValueError):
        with fetcher:
            fetcher.wait_for_first(timeout=1)
            raise ValueError("boom")
    assert not fetcher._thread.is_alive()


def test_stop_wakes_thread_promptly_even_with_long_interval():
    def fetch_fn():
        return SimpleNamespace(tag="v")

    f = BackgroundStateFetcher(fetch_fn, interval=60.0)
    f.start()
    f.wait_for_first(timeout=1)
    t0 = time.monotonic()
    f.stop(join_timeout=2.0)
    assert time.monotonic() - t0 < 1.0


def test_slow_fetch_refetches_immediately_no_extra_sleep():
    starts: list[float] = []
    fetch_duration = 0.10
    interval = 0.02

    def fetch_fn():
        starts.append(time.monotonic())
        time.sleep(fetch_duration)
        return SimpleNamespace(tag="v")

    with BackgroundStateFetcher(fetch_fn, interval=interval, error_tolerance=10.0) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and len(starts) < 4:
            time.sleep(0.01)

    assert len(starts) >= 3
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    assert max(gaps) < fetch_duration + 0.05, f"gaps too large: {gaps}"


def test_transient_exception_logged_at_debug_not_warning(caplog):
    """When transient_check classifies an exception as transient, it is logged
    at DEBUG — not WARNING — so the user never sees the noise."""
    caplog.set_level(logging.DEBUG, logger="seekr_chain")

    class Transient(Exception):
        pass

    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(tag="good")
        if calls["n"] <= 4:
            raise Transient("blip")
        raise RuntimeError("real failure")

    with BackgroundStateFetcher(
        fetch_fn, interval=0.01, error_tolerance=10.0, transient_check=lambda e: isinstance(e, Transient)
    ) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and calls["n"] < 5:
            time.sleep(0.01)
        assert calls["n"] >= 5
        assert f.latest().tag == "good"

    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("transient state fetch failed" in r.message for r in debug_records)
    assert any("state fetch failed" in r.message for r in warning_records)


def test_no_transient_check_defaults_to_warning(caplog):
    """Without transient_check, all exceptions are logged at WARNING."""

    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(tag="good")
        raise RuntimeError("boom")

    with BackgroundStateFetcher(fetch_fn, interval=0.01, error_tolerance=10.0) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline and calls["n"] < 3:
            time.sleep(0.01)

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("state fetch failed" in r.message for r in warning_records)
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert not any("transient state fetch failed" in r.message for r in debug_records)


def test_single_transient_blip_is_silent_then_recovers(caplog):
    """A single transient error that recovers on the next fetch is swallowed at
    DEBUG and the streak resets — no WARNING, no raise."""
    caplog.set_level(logging.DEBUG, logger="seekr_chain")

    class Transient(Exception):
        pass

    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        if calls["n"] == 2:
            raise Transient("blip")
        return SimpleNamespace(tag=f"v{calls['n']}")

    with BackgroundStateFetcher(
        fetch_fn, interval=0.01, error_tolerance=0.2, transient_check=lambda e: isinstance(e, Transient)
    ) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and calls["n"] < 5:
            time.sleep(0.01)
        assert calls["n"] >= 5
        assert f.latest().tag != "v1"

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert not warning_records


def test_continuous_transient_errors_raise_after_tolerance():
    """When transient errors persist continuously past error_tolerance, the
    fetcher raises ContinuousFetchError from latest()."""

    class Transient(Exception):
        pass

    def fetch_fn():
        raise Transient("persistent 401")

    with BackgroundStateFetcher(
        fetch_fn, interval=0.01, error_tolerance=0.1, transient_check=lambda e: isinstance(e, Transient)
    ) as f:
        # Wait for the streak to exceed the tolerance.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with f._lock:
                if f._fatal_error is not None:
                    break
            time.sleep(0.01)
        with f._lock:
            assert f._fatal_error is not None
        # latest() re-raises the fatal error.
        with pytest.raises(ContinuousFetchError) as exc_info:
            f.latest()
        assert exc_info.value.errors
        assert any("persistent 401" in str(e) for e in exc_info.value.errors)


def test_continuous_non_transient_errors_raise_after_tolerance():
    """Non-transient errors also escalate to a raise after the tolerance window."""

    def fetch_fn():
        raise RuntimeError("server 500")

    with BackgroundStateFetcher(fetch_fn, interval=0.01, error_tolerance=0.1) as f:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with f._lock:
                if f._fatal_error is not None:
                    break
            time.sleep(0.01)
        with f._lock:
            assert f._fatal_error is not None
        with pytest.raises(ContinuousFetchError) as exc_info:
            f.latest()
        assert exc_info.value.errors
        assert any("server 500" in str(e) for e in exc_info.value.errors)


def test_success_resets_streak():
    """A successful fetch in the middle of errors resets the streak, so a later
    blip doesn't accumulate toward the tolerance."""

    class Transient(Exception):
        pass

    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        n = calls["n"]
        # pattern: good, blip, good, blip, good, ... never persists long enough
        if n % 2 == 0:
            raise Transient("blip")
        return SimpleNamespace(tag=f"v{n}")

    with BackgroundStateFetcher(
        fetch_fn, interval=0.01, error_tolerance=0.2, transient_check=lambda e: isinstance(e, Transient)
    ) as f:
        f.wait_for_first(timeout=1)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and calls["n"] < 20:
            time.sleep(0.01)
        assert calls["n"] >= 20
        # Never went fatal — streak kept resetting on success.
        with f._lock:
            assert f._fatal_error is None
