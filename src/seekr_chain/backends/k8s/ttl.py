"""Artifact TTL index: mark jobs for deletion and sweep expired ones.

Each launch writes an empty marker to ``<datastore_root>/ttl/<expiry-date>/<workflow_id>``.
A later launch sweeps that index and deletes any job whose expiry date has
passed, reclaiming S3 artifacts without a separate scheduled job.
"""

import dataclasses
import datetime
import logging

from seekr_chain import remote_fs
from seekr_chain.backends.k8s.job_info import get_job_info

logger = logging.getLogger(__name__)

_DATE_FMT = "%Y-%m-%d"


@dataclasses.dataclass
class _ExpiredJob:
    workflow_id: str
    s3_path: str
    marker: str
    artifacts: list[str] | None = None


def write_ttl_marker(
    datastore_root: str, workflow_id: str, artifact_ttl: datetime.timedelta, now: datetime.datetime | None = None
) -> None:
    """Record that `workflow_id`'s artifacts may be deleted on or after now + artifact_ttl."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    expiry = (now + artifact_ttl).date()
    marker = remote_fs.join(datastore_root, "ttl", expiry.strftime(_DATE_FMT), workflow_id)
    remote_fs.touch(marker)


def sweep_expired(datastore_root: str, now: datetime.datetime | None = None) -> int:
    """Delete every job whose TTL marker's date has passed.

    Best-effort: a missing or unreadable TTL index just means nothing to sweep
    yet, so failures here are logged and swallowed rather than raised.

    Returns the number of jobs reclaimed (markers deleted).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    ttl_root = remote_fs.join(datastore_root, "ttl")

    try:
        date_dirs = remote_fs.listdir(ttl_root)
    except Exception as e:
        logger.warning("Skipping TTL sweep: unable to list %s: %s", ttl_root, e)
        return 0

    jobs = _collect_expired_jobs(datastore_root, ttl_root, date_dirs, today)
    if not jobs:
        return 0
    return _delete_jobs(jobs)


def _collect_expired_jobs(
    datastore_root: str, ttl_root: str, date_dirs: list[str], today: datetime.date
) -> list[_ExpiredJob]:
    jobs = []
    for date_str in date_dirs:
        try:
            expiry = datetime.datetime.strptime(date_str, _DATE_FMT).date()
        except ValueError:
            continue
        if expiry > today:
            continue

        date_prefix = remote_fs.join(ttl_root, date_str)
        for workflow_id in remote_fs.listdir(date_prefix):
            s3_path = get_job_info(workflow_id, datastore_root=datastore_root)["s3_path"]
            jobs.append(_ExpiredJob(workflow_id, s3_path, remote_fs.join(date_prefix, workflow_id)))
    return jobs


def _delete_jobs(jobs: list[_ExpiredJob]) -> int:
    """Batch-delete artifacts across all expired jobs, then their markers.

    A job's marker is only deleted once ALL of its artifacts are confirmed
    deleted, so a partial failure never silently orphans S3 artifacts --
    the job simply shows up as expired again next sweep.
    """
    for job in jobs:
        try:
            job.artifacts = remote_fs.list_objects(job.s3_path)
        except Exception as e:
            logger.warning("Skipping TTL job %s: cannot list artifacts: %s", job.workflow_id, e)

    all_artifacts = [uri for job in jobs if job.artifacts is not None for uri in job.artifacts]
    failed = set(remote_fs.delete_many(all_artifacts))

    markers = []
    for job in jobs:
        if job.artifacts is None:
            continue
        if set(job.artifacts) & failed:
            logger.warning("Keeping marker for %s: some artifacts failed to delete", job.workflow_id)
        else:
            markers.append(job.marker)

    remote_fs.delete_many(markers)
    logger.info("TTL sweep deleted %d job(s)", len(markers))
    return len(markers)
