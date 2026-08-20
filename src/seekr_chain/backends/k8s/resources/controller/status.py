"""Outcome-only status.json, written to the shared workspace and shipped to S3
by the controller itself via ``s5cmd``. Both the write and the ship are
best-effort — this must never affect the controller's own control flow, same
pattern as save_phases.

Writes are local and signal a background shipper thread asynchronously, so a
burst of phase transitions never blocks the watch loop; the shipper coalesces
bursts by always uploading the latest on-disk file. At the three workflow-exit
points, the controller flushes synchronously instead, guaranteeing the
terminal status reaches S3 before the process exits.

stdlib only (json/os/subprocess/threading + timeutil, itself stdlib-only):
this module ships standalone into the controller pod, where seekr_chain (and
boto3/kubernetes) is not installed.
"""

import json
import os
import subprocess
import threading

from .phases import TERMINAL_PHASES
from .timeutil import now_iso

# Workspace root (emptyDir mounted rw) — sibling of the assets the chain-init
# container unpacks.
_STATUS_PATH = "/seekr-chain/status.json"

# S3 destination for status.json; unset means shipping is disabled (no-op).
_REMOTE_STATUS_ENV = "SEEKR_CHAIN_REMOTE_STATUS_PATH"

_SHIP_TIMEOUT_SECONDS = 15

# Signals the shipper thread that a fresh write is ready to upload.
_ship_event = threading.Event()
# Serializes s5cmd invocations — never two uploads to the same key at once.
_ship_lock = threading.Lock()
# Guards one-time shipper-thread startup, kept separate from _ship_lock so
# signaling a write never contends with an in-flight upload.
_start_lock = threading.Lock()
_shipper: threading.Thread | None = None


def _derive_status(phases: dict[str, str]) -> str:
    """Roll per-step phases up into a single workflow status.

    Precedence mirrors the client's status derivation: a cancellation trumps a
    failure (both are terminal, but CANCELLED reflects explicit user intent),
    a failure trumps success, and the workflow isn't SUCCEEDED until every
    step has reached a terminal phase.
    """
    values = phases.values()
    if "CANCELLED" in values:
        return "TERMINATED"
    if "FAILED" in values:
        return "FAILED"
    if all(p in TERMINAL_PHASES for p in values):
        return "SUCCEEDED"
    return "RUNNING"


def _build_status(
    workflow_id: str,
    dag: list[dict],
    phases: dict[str, str],
    timings: dict[str, dict],
) -> dict:
    """Assemble the outcome-only status document for one workflow snapshot."""
    return {
        "schema_version": 1,
        "id": workflow_id,
        "status": _derive_status(phases),
        "steps": [
            {
                "name": step["name"],
                "phase": phases.get(step["name"], "PENDING"),
                "dt_start": timings.get(step["name"], {}).get("dt_start"),
                "dt_end": timings.get(step["name"], {}).get("dt_end"),
            }
            for step in dag
        ],
        "captured_at": now_iso(),
    }


def _persist_status_file(
    workflow_id: str,
    dag: list[dict],
    phases: dict[str, str],
    timings: dict[str, dict],
) -> None:
    """Write status.json to the workspace. Best-effort — never raises."""
    try:
        with open(_STATUS_PATH, "w") as f:
            json.dump(_build_status(workflow_id, dag, phases, timings), f)
    except Exception as exc:
        print(f"[controller] warning: could not write status.json: {exc}", flush=True)


def _ship_once() -> None:
    """Upload the on-disk status.json to S3 via s5cmd. No-op if shipping isn't
    configured. Best-effort — never raises (a stuck or failing upload must
    never affect the watch loop)."""
    remote = os.environ.get(_REMOTE_STATUS_ENV)
    if not remote:
        return
    try:
        with _ship_lock:
            subprocess.run(
                ["s5cmd", "cp", _STATUS_PATH, remote],
                timeout=_SHIP_TIMEOUT_SECONDS,
                capture_output=True,
                check=False,
            )
    except Exception as exc:
        print(f"[controller] warning: could not ship status.json to S3: {exc}", flush=True)


def _shipper_loop() -> None:
    """Background loop: upload the latest status.json whenever signaled. A
    burst of writes between wakeups coalesces into a single upload of
    whatever is on disk when the loop wakes."""
    while True:
        _ship_event.wait()
        _ship_event.clear()
        _ship_once()


def _ensure_shipper() -> None:
    """Start the background shipper thread on first use."""
    global _shipper
    with _start_lock:
        if _shipper is None:
            _shipper = threading.Thread(target=_shipper_loop, name="status-shipper", daemon=True)
            _shipper.start()


def write_status(
    workflow_id: str,
    dag: list[dict],
    phases: dict[str, str],
    timings: dict[str, dict],
) -> None:
    """Persist status.json locally and signal the background shipper to
    upload it. Returns immediately — never blocks on the upload."""
    _persist_status_file(workflow_id, dag, phases, timings)
    if os.environ.get(_REMOTE_STATUS_ENV):
        _ensure_shipper()
        _ship_event.set()


def flush_status(
    workflow_id: str,
    dag: list[dict],
    phases: dict[str, str],
    timings: dict[str, dict],
) -> None:
    """Persist status.json locally and upload it synchronously. Used at the
    workflow's terminal exit points to guarantee the final status reaches S3
    before the process exits."""
    _persist_status_file(workflow_id, dag, phases, timings)
    _ship_once()
