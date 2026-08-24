"""Heartbeat file and Kubernetes Event emission for the controller pod."""

import datetime
import time

# Path of the heartbeat file checked by the liveness probe.
_HEARTBEAT_PATH = "/tmp/controller-heartbeat"


def touch_heartbeat() -> None:
    """Touch the heartbeat file to signal the liveness probe that we're alive."""
    try:
        with open(_HEARTBEAT_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def emit_event(
    k8s_v1,
    namespace: str,
    workflow_id: str,
    job_uid: str,
    reason: str,
    message: str,
    event_type: str = "Normal",
) -> None:
    """Emit a Kubernetes Event on the controller Job. Best-effort — never raises."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        k8s_v1.create_namespaced_event(
            namespace=namespace,
            body={
                "apiVersion": "v1",
                "kind": "Event",
                "metadata": {
                    "name": f"{workflow_id}.{reason.lower()}.{int(time.time())}",
                    "namespace": namespace,
                },
                "involvedObject": {
                    "apiVersion": "jobset.x-k8s.io/v1alpha2",
                    "kind": "JobSet",
                    "name": workflow_id,
                    "namespace": namespace,
                    "uid": job_uid,
                },
                "reason": reason,
                "message": message,
                "type": event_type,
                "eventTime": now,
                "reportingComponent": "seekr-chain-controller",
                "reportingInstance": workflow_id,
                "action": reason,
            },
        )
    except Exception as exc:
        print(f"[controller] warning: could not emit event {reason!r}: {exc}", flush=True)
