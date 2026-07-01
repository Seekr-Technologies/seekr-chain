"""
Unit tests for BackgroundStateFetcher.

Uses SimpleNamespace as a stand-in for WorkflowState — the fetcher is
generic over its fetch_fn's return type, so the tests never build a real
WorkflowState.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from seekr_chain.backends.k8s.state_fetcher import BackgroundStateFetcher


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
        # Poll for a later version to arrive; interval is 10 ms so this is quick.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if counter["n"] >= 3:
                break
            time.sleep(0.01)
        assert counter["n"] >= 3
        # latest() should be one of the later versions.
        assert f.latest().tag != "v1"


def test_wait_for_first_times_out_when_fetch_hangs():
    hang = threading.Event()

    def fetch_fn():
        hang.wait()  # never returns during the test
        return SimpleNamespace(tag="never")

    with BackgroundStateFetcher(fetch_fn, interval=0.01) as f:
        with pytest.raises(TimeoutError):
            f.wait_for_first(timeout=0.05)
        hang.set()  # let the thread exit cleanly on stop()


def test_exception_in_fetch_fn_is_swallowed_and_last_good_state_survives(caplog):
    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(tag="good")
        raise RuntimeError("boom")

    with BackgroundStateFetcher(fetch_fn, interval=0.01) as f:
        f.wait_for_first(timeout=1)
        # Let a few failing fetches run.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and calls["n"] < 4:
            time.sleep(0.01)
        assert calls["n"] >= 4
        # Last good state is still served.
        assert f.latest().tag == "good"

    # The exceptions were logged, not raised.
    assert any("state fetch failed" in r.message for r in caplog.records if r.levelname == "WARNING")


def test_stop_is_idempotent():
    def fetch_fn():
        return SimpleNamespace(tag="v")

    f = BackgroundStateFetcher(fetch_fn, interval=0.01)
    f.start()
    f.wait_for_first(timeout=1)
    f.stop()
    f.stop()  # second call should be a no-op, not raise
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
    """The thread waits on the stop event, not on time.sleep — so a large
    interval doesn't delay shutdown."""

    def fetch_fn():
        return SimpleNamespace(tag="v")

    f = BackgroundStateFetcher(fetch_fn, interval=60.0)
    f.start()
    f.wait_for_first(timeout=1)
    t0 = time.monotonic()
    f.stop(join_timeout=2.0)
    assert time.monotonic() - t0 < 1.0
