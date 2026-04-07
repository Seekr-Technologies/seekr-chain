from typing import TypedDict

from seekr_chain import s3_utils
from seekr_chain.user_config import _load_config


def _resolve_datastore_root() -> str | None:
    """Resolve the datastore root from layered configuration.

    Resolution order:
    1. ``SEEKRCHAIN_DATASTORE_ROOT`` environment variable
    2. ``SEEKRCHAIN_DATASTORE_ROOT`` in a ``.env`` file (walk up from CWD)
    3. ``datastore_root`` in ``.seekrchain.toml`` (walk up from CWD)
    4. ``datastore_root`` in ``~/.seekrchain.toml``
    """
    return _load_config().datastore_root


class JobInfo(TypedDict):
    id: str
    s3_path: str
    remote_assets_path: str
    remote_logs_path: str
    remote_sentinel: str
    remote_step_data_path: str
    remote_version_path: str


def get_job_info(id: str, datastore_root: str | None = None) -> JobInfo:
    """
    Get job "info". Probably missnamed. Just a single helper function to get
    paths and other info derived from a job ID

    Args:
        id: Workflow ID
        datastore_root: Root path for datastore (currently only s3:// is supported, e.g. s3://my-bucket/seekr-chain/)
                       Falls back to SEEKRCHAIN_DATASTORE_ROOT env var. Required.
    """
    if datastore_root is None:
        datastore_root = _resolve_datastore_root()

    if datastore_root is None:
        raise ValueError(
            "datastore_root could not be determined. Options (in priority order):\n"
            "  1. Set SEEKRCHAIN_DATASTORE_ROOT environment variable\n"
            "  2. Add to .env file (local overrides, gitignored)\n"
            "  3. Add to .seekrchain.toml in your project root (committed team default)\n"
            "  4. Add to ~/.seekrchain.toml (personal global default)\n"
            "Example: datastore_root = \"s3://my-bucket/seekr-chain/\"\n"
            "When reconnecting to an existing job, use the same value that was set at submit time."
        )

    s3_path = s3_utils.join(datastore_root, "jobs", id[:2], id[2:])

    return JobInfo(
        {
            "id": id,
            "s3_path": s3_path,
            "remote_assets_path": s3_utils.join(s3_path, "assets.tar.gz"),
            "remote_logs_path": s3_utils.join(s3_path, "logs"),
            "remote_sentinel": s3_utils.join(s3_path, ".sentinel"),
            "remote_step_data_path": s3_utils.join(s3_path, "data"),
            "remote_version_path": s3_utils.join(s3_path, "data", "version"),
        }
    )
