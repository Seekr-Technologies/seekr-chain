import datetime

from seekr_chain import remote_fs
from seekr_chain.backends.k8s import ttl

_DATASTORE_ROOT = "s3://bucket/root"


def test_write_ttl_marker_uploads_to_expiry_date_path(monkeypatch):
    recorded = {}

    def fake_touch(path):
        recorded["path"] = path

    monkeypatch.setattr(remote_fs, "touch", fake_touch)

    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    ttl.write_ttl_marker(_DATASTORE_ROOT, "job1", datetime.timedelta(days=90), now=now)

    assert recorded["path"] == "s3://bucket/root/ttl/2026-04-01/job1"


def _fake_listdir_for(mapping):
    def fake_listdir(path):
        return mapping[path]

    return fake_listdir


def _fake_list_objects_for(mapping):
    def fake_list_objects(prefix):
        return mapping[prefix]

    return fake_list_objects


def test_sweep_expired_deletes_artifacts_before_marker(monkeypatch):
    monkeypatch.setattr(
        remote_fs,
        "listdir",
        _fake_listdir_for(
            {
                "s3://bucket/root/ttl": ["2026-01-01"],
                "s3://bucket/root/ttl/2026-01-01": ["job1"],
            }
        ),
    )
    monkeypatch.setattr(
        remote_fs, "list_objects", _fake_list_objects_for({"s3://bucket/root/jobs/jo/b1": ["s3://a", "s3://b"]})
    )
    calls = []
    monkeypatch.setattr(remote_fs, "delete_many", lambda uris: (calls.append(list(uris)), [])[1])

    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    reclaimed = ttl.sweep_expired(_DATASTORE_ROOT, now=now)

    assert calls == [
        ["s3://a", "s3://b"],
        ["s3://bucket/root/ttl/2026-01-01/job1"],
    ]
    assert reclaimed == 1


def test_sweep_expired_keeps_marker_for_job_with_failed_artifact(monkeypatch):
    monkeypatch.setattr(
        remote_fs,
        "listdir",
        _fake_listdir_for(
            {
                "s3://bucket/root/ttl": ["2026-01-01"],
                "s3://bucket/root/ttl/2026-01-01": ["job1", "job2"],
            }
        ),
    )
    monkeypatch.setattr(
        remote_fs,
        "list_objects",
        _fake_list_objects_for(
            {
                "s3://bucket/root/jobs/jo/b1": ["s3://a1"],
                "s3://bucket/root/jobs/jo/b2": ["s3://a2"],
            }
        ),
    )
    calls = []

    def fake_delete_many(uris):
        uris = list(uris)
        calls.append(uris)
        return ["s3://a2"] if uris == ["s3://a1", "s3://a2"] else []

    monkeypatch.setattr(remote_fs, "delete_many", fake_delete_many)

    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    reclaimed = ttl.sweep_expired(_DATASTORE_ROOT, now=now)

    assert calls[1] == ["s3://bucket/root/ttl/2026-01-01/job1"]
    assert reclaimed == 1


def test_sweep_expired_skips_future_date(monkeypatch):
    monkeypatch.setattr(remote_fs, "listdir", _fake_listdir_for({"s3://bucket/root/ttl": ["2099-01-01"]}))
    calls = []
    monkeypatch.setattr(remote_fs, "delete_many", lambda uris: calls.append(list(uris)))

    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    reclaimed = ttl.sweep_expired(_DATASTORE_ROOT, now=now)

    assert calls == []
    assert reclaimed == 0


def test_sweep_expired_ignores_malformed_date_dir(monkeypatch):
    monkeypatch.setattr(remote_fs, "listdir", _fake_listdir_for({"s3://bucket/root/ttl": ["not-a-date"]}))
    calls = []
    monkeypatch.setattr(remote_fs, "delete_many", lambda uris: calls.append(list(uris)))

    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    reclaimed = ttl.sweep_expired(_DATASTORE_ROOT, now=now)

    assert calls == []
    assert reclaimed == 0


def test_sweep_expired_swallows_listdir_failure(monkeypatch):
    def raise_listdir(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(remote_fs, "listdir", raise_listdir)
    calls = []
    monkeypatch.setattr(remote_fs, "delete_many", lambda uris: calls.append(list(uris)))

    reclaimed = ttl.sweep_expired(_DATASTORE_ROOT)

    assert calls == []
    assert reclaimed == 0
