import datetime
import tempfile
from pathlib import Path

from seekr_chain import remote_fs
from seekr_chain.backends.k8s import ttl
from seekr_chain.backends.k8s.job_info import get_job_info


def _upload_fake_artifacts(s3_path: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        f = Path(tmpdir) / "artifact"
        f.write_text("fake")
        remote_fs.upload(f, remote_fs.join(s3_path, "assets.tar.gz"))
        remote_fs.upload(f, remote_fs.join(s3_path, "logs", "x.log"))


class TestTtl:
    def test_sweep_deletes_expired_job_but_keeps_unexpired(self, s3_client, datastore_root):
        expired_id = "expiredx"
        fresh_id = "freshxxx"

        expired_info = get_job_info(expired_id, datastore_root=datastore_root)
        fresh_info = get_job_info(fresh_id, datastore_root=datastore_root)

        _upload_fake_artifacts(expired_info["s3_path"])
        _upload_fake_artifacts(fresh_info["s3_path"])

        now = datetime.datetime.now(datetime.timezone.utc)
        expired_now = now - datetime.timedelta(days=10)
        ttl.write_ttl_marker(datastore_root, expired_id, datetime.timedelta(days=1), now=expired_now)
        ttl.write_ttl_marker(datastore_root, fresh_id, datetime.timedelta(days=90), now=now)

        expired_marker = remote_fs.join(
            datastore_root, "ttl", (expired_now + datetime.timedelta(days=1)).date().strftime("%Y-%m-%d"), expired_id
        )
        fresh_marker = remote_fs.join(
            datastore_root, "ttl", (now + datetime.timedelta(days=90)).date().strftime("%Y-%m-%d"), fresh_id
        )

        ttl.sweep_expired(datastore_root)

        assert not remote_fs.exists(expired_info["s3_path"])
        assert not remote_fs.exists(expired_marker)
        assert remote_fs.exists(fresh_info["s3_path"])
        assert remote_fs.exists(fresh_marker)
