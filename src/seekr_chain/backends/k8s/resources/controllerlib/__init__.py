"""
DAG executor that runs inside the controller pod.

Reads pre-rendered JobSet manifests and dag.json from disk (downloaded from S3
by init containers) and submits them to Kubernetes in dependency order.

Uses the Kubernetes watch API to react immediately when a JobSet reaches a
terminal state, rather than polling on a fixed interval. The watch stream
reconnects automatically on transient errors, resuming from the last seen
resourceVersion so no events are missed.

Reliability features:
  - Phase state is persisted to a ConfigMap after every transition, so a
    restarted controller pod can resume exactly where it left off (rather than
    re-inferring state from 409 Conflict responses alone).
  - Step transitions are emitted as Kubernetes Events, visible via
    ``kubectl describe job <workflow-id>``.
  - A heartbeat file (``/tmp/controller-heartbeat``) is touched at startup and
    after every watch stream iteration.  The Job spec mounts a liveness probe
    that kills the container if the heartbeat goes stale, triggering a pod
    restart and watch-stream reconnect.

Required environment variables:
    SEEKR_CHAIN_JOB_ASSET_PATH        Path where assets were extracted (e.g. /seekr-chain/assets)
    SEEKR_CHAIN_NAMESPACE             Kubernetes namespace for JobSets
    SEEKR_CHAIN_CONTROLLER_JOB_NAME   Name of this controller JobSet (for ownerReferences; its UID
                                      is self-read at startup via the Kubernetes API, since the
                                      JobSet has no downward-API-exposed pod label for it)

Only depends on: Python stdlib + kubernetes + pyyaml
"""

from .watch import main

__all__ = ["main"]
