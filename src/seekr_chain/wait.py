#!/usr/bin/env python3

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import overload

from seekr_chain.status_model import Status
from seekr_chain.workflow import Workflow

logger = logging.getLogger(__name__)


def _format_wait_message(jobs, statuses):
    return "\n".join([f"  {job.name} : {status.value}" for job, status in zip(jobs, statuses)])


def _heartbeat_loop(stop_event: threading.Event, statuses: list, total: int, poll_interval: int) -> None:
    """Log "N/M workflows complete" every poll_interval seconds until stopped.

    watch_controller_status() only yields on a status change, so a long wait
    on a healthy-but-slow job otherwise produces no output between the
    initial submit and the final summary.
    """
    while not stop_event.wait(timeout=poll_interval):
        done = sum(1 for s in statuses if s is not None)
        logger.info(f"{done}/{total} workflows complete")


def _watch_to_completion(job: Workflow) -> Status:
    """Watch a single job via watch_controller_status() and return its final status."""
    for status in job.watch_controller_status():
        if status.is_finished():
            logger.info(f"{job.name}: {status.value}")
            return status
    # Stream ended without a finished status (e.g. Job deleted mid-watch).
    status = job.get_status()
    logger.info(f"{job.name}: watch ended without a finished status, falling back to {status.value}")
    return status


@overload
def wait(jobs: Workflow, poll_interval: int) -> Status: ...
@overload
def wait(jobs: list[Workflow], poll_interval: int) -> list[Status]: ...


def wait(jobs: Workflow | list[Workflow], poll_interval: int = 10) -> Status | list[Status]:
    """Wait for one or more jobs to finish.

    Uses watch_controller_status() on each job for event-driven detection (no fixed poll
    interval for k8s backends). poll_interval is kept for API compatibility but
    is unused when the backend provides a native watch_controller_status() implementation.

    Multiple jobs are watched concurrently — this function blocks until the last
    one finishes.
    """
    is_list = True
    if not isinstance(jobs, list):
        is_list = False
        jobs = [jobs]

    statuses: list[Status | None] = [None] * len(jobs)
    executor = ThreadPoolExecutor(max_workers=max(1, len(jobs)))
    futures = {executor.submit(_watch_to_completion, job): i for i, job in enumerate(jobs)}

    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, args=(stop_heartbeat, statuses, len(jobs), poll_interval), daemon=True
    )
    heartbeat_thread.start()

    try:
        for future in as_completed(futures):
            i = futures[future]
            statuses[i] = future.result()
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)
        # wait=False: a `with ThreadPoolExecutor()` block's __exit__ calls
        # shutdown(wait=True), which blocks until every submitted future
        # finishes — including ones still watching a healthy job for hours.
        # That would swallow the whole point of fast-failing on one job's
        # WatchStalledError. cancel_futures drops any not-yet-started
        # futures; already-running ones keep running in the background but
        # don't block this call from returning/raising immediately.
        executor.shutdown(wait=False, cancel_futures=True)

    logger.info(f"All {len(jobs)} workflows complete\n{_format_wait_message(jobs, statuses)}")

    return statuses[0] if not is_list else statuses
