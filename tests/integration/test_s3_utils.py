#!/usr/bin/env python3

import datetime
import re
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import dateutil
import pytest

from seekr_chain import s3_utils


@pytest.fixture
def s3_tmpdir(s3_client, unique_test_name, minio_service):
    bucket = "seekr-chain-test" if minio_service is not None else "seekr-ml-taw"
    base_prefix = "_seekr_chain_unittests"

    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "/", unique_test_name).strip("-").strip("/")
    safe_name = safe_name.replace("/./", "/")

    uuid = uuid4().hex
    prefix = "/".join([base_prefix, safe_name, uuid[:2], uuid[2:]])

    uri = f"s3://{bucket}/{prefix}/"
    try:
        yield uri

    finally:
        # Best-effort cleanup of everything under the prefix
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        keys = []
        for page in pages:
            for obj in page.get("Contents", []):
                keys.append({"Key": obj["Key"]})
                # Delete in batches of up to 1000
                if len(keys) == 1000:
                    s3_client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
                    keys.clear()

        if keys:
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


def populate(s3_client, prefix: str, items: dict[str, str | bytes | int]) -> None:
    """
    Create objects under `prefix` for each name->contents in `items`.

    - `prefix`: 's3://bucket/path/' or 'bucket/path/' (trailing slash optional)
    - Values MUST be `str` or `bytes` (use '' or b'' for empty objects)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        for key, value in items.items():
            local_path = p / key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                with open(local_path, "w") as f:
                    f.write(value)
            elif isinstance(value, bytes):
                with open(local_path, "wb") as f:
                    f.write(value)
            elif isinstance(value, int):
                with open(local_path, "wb") as f:
                    f.truncate(value)
            else:
                raise TypeError("")

        s3_utils.upload_dir(p, prefix, s3_client)


class TestBasicFSOps:
    """Test basic FS ops (exists, is_file, is_dir, etc).

    Test these together because they rely on eachother"""

    def test_exists_file(self, s3_client, s3_tmpdir):
        dest = s3_utils.join(s3_tmpdir, "file")

        assert not s3_utils.exists(dest, s3_client)
        assert not s3_utils.is_file(dest, s3_client)
        assert not s3_utils.is_dir(dest, s3_client)

        s3_utils.touch(dest, s3_client)

        assert s3_utils.exists(dest, s3_client)
        assert s3_utils.is_file(dest, s3_client)
        assert not s3_utils.is_dir(dest, s3_client)

    def test_exists_dir_marker(self, s3_client, s3_tmpdir):
        dest = s3_utils.join(s3_tmpdir, "dir_marker/")

        assert not s3_utils.exists(dest, s3_client)
        assert not s3_utils.is_file(dest, s3_client)
        assert not s3_utils.is_dir(dest, s3_client)

        bucket, key = s3_utils.parse_s3_uri(dest)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,  # trailing slash is the usual folder-marker style
            Body=b"",  # zero bytes
            ContentType="application/x-directory; charset=UTF-8",
        )

        assert s3_utils.exists(dest, s3_client)
        assert not s3_utils.is_file(dest, s3_client)
        assert s3_utils.is_dir(dest, s3_client)

    def test_exists_dir(self, s3_client, s3_tmpdir):
        contents = {
            "dir/file0": "a",
            "dir/file1": "b",
        }
        dest = s3_utils.join(s3_tmpdir, "dir")

        assert not s3_utils.exists(dest, s3_client)
        assert not s3_utils.is_file(dest, s3_client)
        assert not s3_utils.is_dir(dest, s3_client)

        populate(s3_client, s3_tmpdir, contents)

        assert s3_utils.exists(dest, s3_client)
        assert not s3_utils.is_file(dest, s3_client)
        assert s3_utils.is_dir(dest, s3_client)

    def test_touch(self, s3_client, s3_tmpdir):
        dest = s3_utils.join(s3_tmpdir, "file")

        t0 = datetime.datetime.now(datetime.timezone.utc)
        time.sleep(1)
        assert not s3_utils.exists(dest, s3_client)

        s3_utils.touch(dest, s3_client)

        assert s3_utils.is_file(dest, s3_client)

        stat = s3_utils.stat(dest, s3_client)

        assert stat.size == 0
        assert stat.mtime > t0

        time.sleep(1)

        # Touch again, check mtime updated
        s3_utils.touch(dest, s3_client)
        stat_2 = s3_utils.stat(dest, s3_client)

        assert stat_2.mtime > stat.mtime

    def test_delete(self, s3_client, s3_tmpdir):
        contents = {
            "file0": "",
            "dir/file0": "",
            "dir/file1": "",
            "dir/file2": "",
        }

        populate(s3_client, s3_tmpdir, contents)

        fpath = s3_utils.join(s3_tmpdir, "file0")
        dir_path = s3_utils.join(s3_tmpdir, "dir/")
        dir_fpath = s3_utils.join(s3_tmpdir, "dir", "file0")

        # test deleting a file
        assert s3_utils.is_file(fpath, s3_client)
        s3_utils.delete(fpath, s3_client)
        assert not s3_utils.is_file(fpath, s3_client)

        # Test deleting file in dir, dir still exists
        assert s3_utils.is_dir(dir_path, s3_client)
        assert s3_utils.is_file(dir_fpath, s3_client)
        s3_utils.delete(dir_fpath, s3_client)
        assert s3_utils.is_dir(dir_path, s3_client)
        assert not s3_utils.is_file(dir_fpath, s3_client)

        # Test deleting dir
        assert s3_utils.is_dir(dir_path, s3_client)
        s3_utils.delete(dir_path, s3_client)
        assert not s3_utils.is_dir(dir_path, s3_client)

    def test_stat(self, s3_client, s3_tmpdir):
        t0 = datetime.datetime.now(datetime.timezone.utc)
        time.sleep(1)

        contents = {
            "file0": b"000000",
        }

        populate(s3_client, s3_tmpdir, contents)

        stat = s3_utils.stat(s3_utils.join(s3_tmpdir, "file0"), s3_client)

        assert stat.size == 6
        assert isinstance(stat.mtime, datetime.datetime)
        assert stat.mtime.tzinfo is dateutil.tz.tzutc()
        dt = (stat.mtime - t0).total_seconds()
        assert 0 < dt < 100


class TestGlob:
    @pytest.mark.parametrize(
        "pattern,expected",
        [
            # Top-level only
            ("*", ["a.txt", "b.txt"]),
            ("*.txt", ["a.txt", "b.txt"]),
            # Single-level under a dir
            ("dir/*", ["dir/x.txt", "dir/y.csv", "dir/.hidden"]),
            ("dir/*.txt", ["dir/x.txt"]),
            ("dir/*.*", ["dir/.hidden", "dir/x.txt", "dir/y.csv"]),
            ("dir/?.csv", ["dir/y.csv"]),  # single-char match
            # Recursive matches
            ("dir/**/*.txt", ["dir/x.txt", "dir/nested/z.txt"]),
            ("**/nested/*.txt", ["dir/nested/z.txt"]),
            ("**/*.md", ["docs/readme.md"]),  # any depth, markdown
            # One directory deep (not top-level)
            ("*/a.txt", ["dir2/a.txt"]),
            # Hidden files (explicit)
            ("dir2/.*", ["dir2/.hidden2"]),
            # Spaces and unicode
            ("dir space/*.txt", ["dir space/with space.txt"]),
            ("unicode/*", ["unicode/über.txt"]),
        ],
    )
    def test_basic(self, s3_client, s3_tmpdir, pattern, expected):
        contents = {
            # top-level
            "a.txt": "a",
            "b.txt": "b",
            # under dir/
            "dir/x.txt": "x",
            "dir/y.csv": "c",
            "dir/.hidden": "h",
            "dir/nested/z.txt": "z",
            # another dir
            "dir2/a.txt": "a2",
            "dir2/.hidden2": "h2",
            # recursive markdown somewhere else
            "docs/readme.md": "# hi",
            # spaces + unicode
            "dir space/with space.txt": "ws",
            "unicode/über.txt": "u",
        }
        populate(s3_client, s3_tmpdir, contents)

        result = s3_utils.glob(s3_tmpdir, pattern, s3_client)
        result = [item.removeprefix(s3_tmpdir) for item in result]

        assert set(result) == set(expected)
