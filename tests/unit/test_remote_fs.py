"""Unit tests for seekr_chain.remote_fs: scheme dispatch, URI parsing, and
client caching. Backend calls are mocked -- no real S3/OCI traffic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from seekr_chain import remote_fs


@pytest.fixture(autouse=True)
def _reset_cached_clients():
    remote_fs._s3_client = None
    remote_fs._oci_client = None
    yield
    remote_fs._s3_client = None
    remote_fs._oci_client = None


class TestJoin:
    def test_joins_with_single_slash(self):
        assert remote_fs.join("s3://bucket/prefix", "a", "b") == "s3://bucket/prefix/a/b"

    def test_strips_redundant_slashes(self):
        assert remote_fs.join("s3://bucket/prefix/", "/a/", "/b") == "s3://bucket/prefix/a/b"

    def test_preserves_trailing_slash_on_last_part(self):
        assert remote_fs.join("s3://bucket/prefix", "a/") == "s3://bucket/prefix/a/"


class TestParseUri:
    def test_s3(self):
        assert remote_fs.parse_uri("s3://my-bucket/some/key") == ("my-bucket", "some/key")

    def test_oci(self):
        assert remote_fs.parse_uri("oci://ns/bucket/some/key") == ("ns", "bucket", "some/key")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.parse_uri("gs://bucket/key")


class TestDispatch:
    """upload/download/exists must route to the right backend by scheme."""

    def test_upload_unsupported_scheme_raises(self, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("hi")
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.upload(src, "gs://bucket/key")

    def test_download_unsupported_scheme_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.download("gs://bucket/key", tmp_path / "out")

    def test_exists_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.exists("gs://bucket/key")

    def test_upload_routes_to_s3(self, monkeypatch, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("hi")
        seen = {}
        monkeypatch.setattr(remote_fs, "_s3_upload", lambda s, d: seen.update(src=s, dst=d))
        remote_fs.upload(src, "s3://bucket/key")
        assert seen == {"src": src, "dst": "s3://bucket/key"}

    def test_upload_routes_to_oci(self, monkeypatch, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("hi")
        seen = {}
        monkeypatch.setattr(remote_fs, "_oci_upload", lambda s, d: seen.update(src=s, dst=d))
        remote_fs.upload(src, "oci://ns/bucket/key")
        assert seen == {"src": src, "dst": "oci://ns/bucket/key"}


class TestS3ClientCaching:
    def test_client_constructed_once_and_reused(self, monkeypatch):
        made = []

        def fake_boto3_client(service):
            made.append(service)
            return MagicMock()

        monkeypatch.setattr("boto3.client", fake_boto3_client)

        c1 = remote_fs._get_s3_client()
        c2 = remote_fs._get_s3_client()
        assert c1 is c2
        assert made == ["s3"]


class TestOciClientCaching:
    def test_client_constructed_once_and_reused(self, monkeypatch):
        made = []

        def fake_build():
            made.append(1)
            return MagicMock()

        monkeypatch.setattr(remote_fs, "_build_oci_client", fake_build)

        c1 = remote_fs._get_oci_client()
        c2 = remote_fs._get_oci_client()
        assert c1 is c2
        assert len(made) == 1


class TestS3Exists:
    def test_exists_true_when_file_present(self, monkeypatch):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 5, "ContentType": "text/plain"}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)
        assert remote_fs.exists("s3://bucket/key") is True

    def test_exists_false_on_404(self, monkeypatch):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        client.list_objects_v2.return_value = {}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)
        assert remote_fs.exists("s3://bucket/key") is False

    def test_exists_true_for_prefix_with_contents(self, monkeypatch):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        client.list_objects_v2.return_value = {"Contents": [{"Key": "prefix/a"}]}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)
        assert remote_fs.exists("s3://bucket/prefix") is True


class TestOciExists:
    def test_exists_true_on_success(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)
        assert remote_fs.exists("oci://ns/bucket/key") is True

    def test_exists_false_on_any_error(self, monkeypatch):
        client = MagicMock()
        client.head_object.side_effect = RuntimeError("nope")
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)
        assert remote_fs.exists("oci://ns/bucket/key") is False


class TestS3Upload:
    def test_uploads_file(self, monkeypatch, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("hi")
        client = MagicMock()
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        remote_fs.upload(src, "s3://bucket/key")

        client.upload_file.assert_called_once_with(str(src), "bucket", "key")

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            remote_fs.upload(tmp_path / "nope.txt", "s3://bucket/key")


class TestOciUpload:
    def test_uploads_file(self, monkeypatch, tmp_path):
        src = tmp_path / "f.txt"
        src.write_text("hi")
        client = MagicMock()
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)

        remote_fs.upload(src, "oci://ns/bucket/key")

        assert client.put_object.call_count == 1
        _, kwargs = client.put_object.call_args
        assert kwargs["namespace_name"] == "ns"
        assert kwargs["bucket_name"] == "bucket"
        assert kwargs["object_name"] == "key"


class TestS3Download:
    def test_downloads_single_file(self, monkeypatch, tmp_path):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 5, "ContentType": "text/plain"}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        dst = tmp_path / "out.txt"
        remote_fs.download("s3://bucket/key", dst)

        client.download_file.assert_called_once_with("bucket", "key", str(dst))

    def test_recurses_into_prefix(self, monkeypatch, tmp_path):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "prefix/a.txt", "Size": 0}, {"Key": "prefix/sub/b.txt", "Size": 0}]}
        ]
        client.get_paginator.return_value = paginator

        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        downloaded = []

        def fake_download_file(self, bucket, key, filename):
            downloaded.append((bucket, key, filename))
            Path(filename).touch()

        monkeypatch.setattr(remote_fs.S3Transfer, "download_file", fake_download_file)

        remote_fs.download("s3://bucket/prefix", tmp_path)

        assert sorted(k for _, k, _ in downloaded) == ["prefix/a.txt", "prefix/sub/b.txt"]


class TestS3Delete:
    def test_deletes_single_file(self, monkeypatch):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 5, "ContentType": "text/plain"}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        remote_fs.delete("s3://bucket/key")

        client.delete_object.assert_called_once_with(Bucket="bucket", Key="key")
        client.delete_objects.assert_not_called()

    def test_deletes_prefix(self, monkeypatch):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")

        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": [{"Key": "prefix/a.txt"}, {"Key": "prefix/sub/b.txt"}]}]
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        remote_fs.delete("s3://bucket/prefix")

        client.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={"Objects": [{"Key": "prefix/a.txt"}, {"Key": "prefix/sub/b.txt"}]},
        )
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="prefix/")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.delete("gs://bucket/key")


class TestS3ListDir:
    def test_lists_immediate_children(self, monkeypatch):
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "CommonPrefixes": [{"Prefix": "prefix/sub/"}],
                "Contents": [{"Key": "prefix/a.txt"}, {"Key": "prefix/"}],
            }
        ]
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        result = remote_fs.listdir("s3://bucket/prefix")

        assert result == ["sub", "a.txt"]
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="prefix/", Delimiter="/")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.listdir("gs://bucket/key")


class TestS3Touch:
    def test_creates_empty_object(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        remote_fs.touch("s3://b/k")

        client.put_object.assert_called_once_with(Bucket="b", Key="k", Body=b"")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.touch("gs://bucket/key")


class TestS3ListObjects:
    def test_returns_full_uris(self, monkeypatch):
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Contents": [{"Key": "prefix/a.txt"}, {"Key": "prefix/sub/b.txt"}]}]
        client.get_paginator.return_value = paginator
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        result = remote_fs.list_objects("s3://bucket/prefix")

        assert result == ["s3://bucket/prefix/a.txt", "s3://bucket/prefix/sub/b.txt"]
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="prefix/")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.list_objects("gs://bucket/key")


class TestS3DeleteMany:
    def test_deletes_keys_in_same_bucket(self, monkeypatch):
        client = MagicMock()
        client.delete_objects.return_value = {}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        failed = remote_fs.delete_many(["s3://b/k1", "s3://b/k2"])

        client.delete_objects.assert_called_once_with(Bucket="b", Delete={"Objects": [{"Key": "k1"}, {"Key": "k2"}]})
        assert failed == []

    def test_returns_failed_uris(self, monkeypatch):
        client = MagicMock()
        client.delete_objects.return_value = {"Errors": [{"Key": "k2"}]}
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        failed = remote_fs.delete_many(["s3://b/k1", "s3://b/k2"])

        assert failed == ["s3://b/k2"]

    def test_empty_input_is_noop(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(remote_fs, "_get_s3_client", lambda: client)

        failed = remote_fs.delete_many([])

        client.delete_objects.assert_not_called()
        assert failed == []

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            remote_fs.delete_many(["gs://bucket/key"])


class TestOciDownload:
    def test_downloads_file(self, monkeypatch, tmp_path):
        client = MagicMock()
        resp = MagicMock()
        resp.data.raw.stream.return_value = [b"hello"]
        client.get_object.return_value = resp
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)

        dst = tmp_path / "out.txt"
        remote_fs.download("oci://ns/bucket/key", dst)

        assert dst.read_bytes() == b"hello"

    def test_raises_clear_error_on_directory(self, monkeypatch, tmp_path):
        not_found = Exception("not found")
        not_found.status = 404

        client = MagicMock()
        client.get_object.side_effect = not_found
        client.list_objects.return_value.data.objects = [MagicMock()]
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)

        with pytest.raises(NotImplementedError, match="directory/prefix"):
            remote_fs.download("oci://ns/bucket/some/dir", tmp_path / "out.txt")

    def test_reraises_plain_404_when_nothing_under_prefix(self, monkeypatch, tmp_path):
        not_found = Exception("not found")
        not_found.status = 404

        client = MagicMock()
        client.get_object.side_effect = not_found
        client.list_objects.return_value.data.objects = []
        monkeypatch.setattr(remote_fs, "_get_oci_client", lambda: client)

        with pytest.raises(Exception, match="not found"):
            remote_fs.download("oci://ns/bucket/missing", tmp_path / "out.txt")
