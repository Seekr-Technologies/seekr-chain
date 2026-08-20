"""Outcome-only status.json, written to the shared workspace for the s5cmd
sidecar to sync to S3. Dark launch: nothing reads this yet (see PR4), so it
must never affect the controller's own control flow — writing it is
best-effort, same pattern as _save_phases.

stdlib only (json + datetime): this module ships standalone into the
controller pod, where seekr_chain (and boto3/kubernetes) is not installed.
"""

import datetime
import json

from .phases import _TERMINAL_PHASES

# Workspace root (emptyDir mounted rw) — sibling of the assets the chain-init
# container unpacks. The status-sync sidecar tails this path and uploads it.
_STATUS_PATH = "/seekr-chain/status.json"


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
    if all(p in _TERMINAL_PHASES for p in values):
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
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _write_status(
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
