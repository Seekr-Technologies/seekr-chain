#!/usr/bin/env python3

from __future__ import annotations

import datetime
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import botocore
from boto3.s3.transfer import S3Transfer, TransferConfig
from botocore.client import BaseClient

from seekr_chain import utils

logger = logging.getLogger(__name__)


def _glob_match(paths: list[str], pattern: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        for item in paths:
            item_path = tmpdir / item
            item_path.parent.mkdir(exist_ok=True, parents=True)
            item_path.touch()

        result = [str(item.relative_to(tmpdir)) for item in tmpdir.glob(pattern) if not item.is_dir()]

    return result


def join(*args: str) -> str:
    """
    Join parts of an S3 path, ensuring exactly one slash between components.

    The first part must be an S3 URI starting with 's3://'.
    """
    # if not args or not args[0].startswith("s3://"):
    #     raise ValueError("First argument must be a full S3 URI starting with 's3://'")

    base = args[0].rstrip("/")
    rest = [arg.strip("/") for arg in args[1:]]

    result = "/".join([base] + rest)
    if args[-1][-1] == "/":
        result += "/"
    return result


def is_s3_path(path: str | Path) -> bool:
    return str(path).startswith("s3://")


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    match = re.match(r"s3://([^/]+)/?(.*)", s3_uri)
    if not match:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    bucket, key = match.groups()
    return bucket, key


def is_file(path: str, s3_client: BaseClient) -> bool:
    """
    Returns True if the object at the given S3 path exists.
    """
    bucket, key = parse_s3_uri(path)

    if not key:
        # Bucket itself is not an object
        return False
    if key.endswith("/"):
        # Anything ending with '/' is automatically assumed to be a dir
        return False

    try:
        resp = s3_client.head_object(Bucket=bucket, Key=key)

        # We can get response for empty "folder markers" created by aws Console.
        # Heuristic: ignore console-created folder markers
        is_dir_marker = resp.get("ContentLength", 0) == 0 and (resp.get("ContentType") or "").lower().startswith(
            "application/x-directory"
        )
        return not is_dir_marker

    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def is_dir(path: str, s3_client: BaseClient) -> bool:
    """
    Returns True if the given S3 path is a prefix that has at least one object.
    """
    bucket, prefix = parse_s3_uri(path)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return "Contents" in resp


def exists(path: str, s3_client: BaseClient) -> bool:
    return is_file(path, s3_client) or is_dir(path, s3_client)


def touch(path: str, s3_client: BaseClient):
    bucket, key = parse_s3_uri(path)
    # Always put a zero-byte object. This creates or updates Last-Modified.
    # (copy_object to self is rejected by MinIO when nothing changes.)
    return s3_client.put_object(Bucket=bucket, Key=key, Body=b"")


def upload_file(src: str | Path, dst: str, s3_client: BaseClient) -> None:
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"Source is not file: {src}")
    bucket, key = parse_s3_uri(dst)

    s3_client.upload_file(str(src), bucket, key)


def download_file(src: str, dst: str, Path, s3_client: BaseClient) -> None:
    dst = Path(dst)
    bucket, key = parse_s3_uri(src)

    s3_client.download_file(bucket, key, str(dst))


def _download_file_helper(
    bucket: str, key: str, prefix: str, dst: Path, transfer: S3Transfer, expected_size: int, sync: bool = False
) -> int:
    rel_path = Path(key).relative_to(prefix)
    local_path = dst / rel_path
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if local_path.exists() and local_path.stat().st_size == expected_size:
        return 0

    transfer.download_file(bucket=bucket, key=key, filename=local_path)
    return local_path.stat().st_size


def download_dir(
    src: str,
    dst: str | Path,
    s3_client,
    *,
    sync: bool = False,
    workers: int | None = None,
    max_concurrency: int = 8,
    multipart_chunksize: int | str = "16Mi",
    multipart_threshold: int | str = "16Mi",
) -> int:
    """
    Recursively upload a directory to S3 as fast as possible.

    Args:
        src: local directory
        dst: S3 URI "bucket/prefix/" (prefix may be empty)
        s3_client: boto3 S3 client
        workers: threads for cross-file concurrency (default: ~2*CPU capped)
        max_concurrency: per-upload concurrency (boto3 multipart worker threads)
        multipart_chunksize: part size for multipart uploads
        multipart_threshold: files >= threshold use multipart
    """
    if workers is None:
        workers = 2 * (os.cpu_count() or 4)

    if isinstance(multipart_threshold, str):
        multipart_threshold = utils.human_to_int(multipart_threshold)
    if isinstance(multipart_chunksize, str):
        multipart_chunksize = utils.human_to_int(multipart_chunksize)

    transfer = S3Transfer(
        client=s3_client,
        config=TransferConfig(
            multipart_threshold=multipart_threshold,
            multipart_chunksize=multipart_chunksize,
            max_concurrency=max_concurrency,
            use_threads=True,
        ),
    )

    bucket, prefix = parse_s3_uri(src)
    dst = Path(dst)
    paginator = s3_client.get_paginator("list_objects_v2")

    total_size = 0
    futures = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                futures.append(
                    ex.submit(
                        _download_file_helper,
                        bucket=bucket,
                        key=obj["Key"],
                        prefix=prefix,
                        dst=dst,
                        transfer=transfer,
                        expected_size=obj["Size"],
                        sync=sync,
                    )
                )
    for fut in as_completed(futures):
        total_size += fut.result()
    return total_size


def upload_dir(src: str | Path, dst: str, s3_client: BaseClient) -> None:
    """
    Upload the given directory recursively to s3.

    :param src: Local source directory to upload
    :param dst: S3 URI in the form "bucket-name/prefix/"
    :param s3_client: boto3 S3 client
    """
    src = Path(src)
    bucket, prefix = parse_s3_uri(dst)

    for root, _, files in os.walk(src):
        for file in files:
            local_path = Path(root) / file
            relative_path = local_path.relative_to(src)
            s3_key = str(Path(prefix) / relative_path).replace("\\", "/")  # for Windows compatibility
            try:
                s3_client.upload_file(str(local_path), bucket, s3_key)
            except botocore.exceptions.ClientError as e:
                logger.error(f"Failed to upload {local_path}: {e}")
                raise


def glob(path: str, pattern: str, s3_client: BaseClient) -> list[str]:
    """
    List all objects in an s3 path.

    Pattern can be any glob. For example, `*`, `**/*`, `*.txt`, etc.
    """
    bucket, prefix = parse_s3_uri(path)

    paginator = s3_client.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    matches = []
    rel_keys = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel_key = str(PurePosixPath(key).relative_to(prefix)) if prefix else key
            rel_keys.append(rel_key)
            # if regex.match(rel_key):
            #     matches.append(f"s3://{bucket}/{key}")

    matches = _glob_match(rel_keys, pattern)
    matches = [join(path, item) for item in matches]

    return matches


@dataclass
class StatDict:
    size: int
    mtime: datetime.datetime


def stat(src: str, s3_client: BaseClient) -> StatDict:
    bucket, key = parse_s3_uri(src)

    result = s3_client.head_object(Bucket=bucket, Key=key)

    out = StatDict(
        size=result["ContentLength"],
        mtime=result["LastModified"],
    )

    return out


def delete(path: str, s3_client: BaseClient) -> int:
    """Delete the object, or all objects under the given prefix

    Returns the total number of objects deleted
    """
    bucket, key = parse_s3_uri(path)

    if path.endswith("/"):
        # Treat as prefix
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=key)

        delete_buffer = []
        total_deleted = 0
        CHUNK_SIZE = 1000

        for page in pages:
            for obj in page.get("Contents", []):
                delete_buffer.append({"Key": obj["Key"]})
                while len(delete_buffer) >= CHUNK_SIZE:
                    chunk = delete_buffer[:CHUNK_SIZE]
                    total_deleted += len(chunk)
                    delete_buffer = delete_buffer[CHUNK_SIZE:]
                    s3_client.delete_objects(Bucket=bucket, Delete={"Objects": chunk})

        if delete_buffer:
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": delete_buffer})
            total_deleted += len(delete_buffer)

        return total_deleted
    else:
        # Treat as a single object
        try:
            s3_client.delete_object(Bucket=bucket, Key=key)
            return 1
        except s3_client.exceptions.NoSuchKey:
            return 0
