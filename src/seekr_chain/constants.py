from pathlib import Path

JOB_ROOT = "/seekr-chain"
JOB_WORKSPACE = JOB_ROOT + "/workspace"
JOB_ASSET_PATH = JOB_ROOT + "/assets"
JOB_ASSET_TAR_PATH = JOB_ROOT + "/assets.tar.gz"
JOB_LOGS_PATH = JOB_ROOT + "/logs.txt"
JOB_RESOURCES_PATH = JOB_ROOT + "/resources"
JOB_SHUTDOWN_PATH = JOB_ROOT + "/.shutdown"

HOSTFILE_PATH = JOB_ROOT + "/hostfile"
PEERMAP_PATH = JOB_ROOT + "/peermap.json"
ENTRYPOINT_PATH = JOB_ROOT + "/entrypoint.sh"
FLUENTBIT_PATH = JOB_ASSET_PATH + "/fluent-bit.conf"
ARGS_PATH = JOB_ASSET_PATH + "/workflow_args.json"

LOCAL_CACHE = Path.home() / ".seekr-chain"
LOCAL_LOG_PATH = LOCAL_CACHE / "logs"
