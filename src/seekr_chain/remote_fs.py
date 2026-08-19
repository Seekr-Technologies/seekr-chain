"""Minimal upload/download/exists for s3:// and oci:// paths.

Consolidates what used to be a standalone `s3_utils.py` plus a parallel,
OCI-only implementation embedded in `nix_utils.py`. Only implements the
operations this repo actually needs (asset/log transfer, closure-cache
existence checks) rather than a general-purpose filesystem abstraction.

Backend clients (`boto3` S3 client, `oci` ObjectStorageClient) are expensive
to construct and safe to reuse, so each is built lazily on first use and
cached in a module-level global for the life of the process.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from boto3.s3.transfer import S3Transfer, TransferConfig
from botocore.exceptions import ClientError

from seekr_chain import utils

_s3_client = None
_oci_client = None


def _scheme(uri: str) -> str:
    return uri.split("://", 1)[0] if "://" in uri else ""


def upload(src: str | Path, dst: str) -> None:
    """Upload the local file at `src` to the remote URI `dst`."""
    scheme = _scheme(dst)
    if scheme == "s3":
        return _s3_upload(src, dst)
    if scheme == "oci":
        return _oci_upload(src, dst)
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def download(src: str, dst: str | Path) -> None:
    """Download the remote URI `src` to the local path `dst`.

    If `src` is a file, downloads just that file. If it's a prefix/directory,
    recursively downloads everything under it.
    """
    scheme = _scheme(src)
    if scheme == "s3":
        return _s3_download(src, dst)
    if scheme == "oci":
        return _oci_download(src, dst)
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def exists(path: str) -> bool:
    """Return True if `path` refers to an existing object or prefix."""
    scheme = _scheme(path)
    if scheme == "s3":
        return _s3_exists(path)
    if scheme == "oci":
        return _oci_exists(path)
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def delete(path: str) -> None:
    """Delete the object or prefix at `path`. No-op if nothing exists there."""
    scheme = _scheme(path)
    if scheme == "s3":
        return _s3_delete(path)
    if scheme == "oci":
        raise NotImplementedError("delete not supported for oci://")
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def listdir(path: str) -> list[str]:
    """Return the immediate children (names, not full URIs) of `path`."""
    scheme = _scheme(path)
    if scheme == "s3":
        return _s3_listdir(path)
    if scheme == "oci":
        raise NotImplementedError("listdir not supported for oci://")
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def touch(path: str) -> None:
    """Create an empty object at `path`."""
    scheme = _scheme(path)
    if scheme == "s3":
        return _s3_touch(path)
    if scheme == "oci":
        raise NotImplementedError("touch not supported for oci://")
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def list_objects(prefix: str) -> list[str]:
    """Return the full URIs of every object recursively under `prefix`."""
    scheme = _scheme(prefix)
    if scheme == "s3":
        return _s3_list_objects(prefix)
    if scheme == "oci":
        raise NotImplementedError("list_objects not supported for oci://")
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def delete_many(uris: list[str]) -> list[str]:
    """Delete many objects, returning the URIs that failed to delete.

    All `uris` are assumed to share a scheme; dispatch is based on the first.
    """
    if not uris:
        return []
    scheme = _scheme(uris[0])
    if scheme == "s3":
        return _s3_delete_many(uris)
    if scheme == "oci":
        raise NotImplementedError("delete_many not supported for oci://")
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def join(*parts: str) -> str:
    """Join path parts with exactly one slash between components.

    The first part is expected to be a full URI (e.g. "s3://bucket/prefix").
    """
    base = parts[0].rstrip("/")
    rest = [p.strip("/") for p in parts[1:]]
    result = "/".join([base] + rest)
    if parts[-1].endswith("/"):
        result += "/"
    return result


def parse_uri(uri: str) -> tuple:
    """Parse a remote URI into its scheme-specific components."""
    scheme = _scheme(uri)
    if scheme == "s3":
        return _parse_s3_uri(uri)
    if scheme == "oci":
        return _parse_oci_uri(uri)
    raise ValueError(f"Unsupported scheme: {scheme!r}")


# --- S3 -----------------------------------------------------------------


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3")
    return _s3_client


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    match = re.match(r"s3://([^/]+)/?(.*)", uri)
    if not match:
        raise ValueError(f"Invalid S3 URI: {uri}")
    bucket, key = match.groups()
    return bucket, key


def _s3_is_file(path: str) -> bool:
    client = _get_s3_client()
    bucket, key = _parse_s3_uri(path)

    if not key or key.endswith("/"):
        return False

    try:
        resp = client.head_object(Bucket=bucket, Key=key)
        # Ignore console-created zero-byte "folder marker" objects.
        is_dir_marker = resp.get("ContentLength", 0) == 0 and (resp.get("ContentType") or "").lower().startswith(
            "application/x-directory"
        )
        return not is_dir_marker
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def _s3_is_dir(path: str) -> bool:
    client = _get_s3_client()
    bucket, prefix = _parse_s3_uri(path)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return "Contents" in resp


def _s3_exists(path: str) -> bool:
    return _s3_is_file(path) or _s3_is_dir(path)


def _s3_upload(src: str | Path, dst: str) -> None:
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"Source is not a file: {src}")
    bucket, key = _parse_s3_uri(dst)
    _get_s3_client().upload_file(str(src), bucket, key)


def _s3_download_file_helper(bucket: str, key: str, prefix: str, dst: Path, transfer: S3Transfer, expected_size: int):
    rel_path = Path(key).relative_to(prefix)
    local_path = dst / rel_path
    local_path.parent.mkdir(parents=True, exist_ok=True)

    if local_path.exists() and local_path.stat().st_size == expected_size:
        return 0

    transfer.download_file(bucket=bucket, key=key, filename=local_path)
    return local_path.stat().st_size


def _s3_download_dir(
    src: str,
    dst: str | Path,
    *,
    workers: int | None = None,
    max_concurrency: int = 8,
    multipart_chunksize: int | str = "16Mi",
    multipart_threshold: int | str = "16Mi",
) -> int:
    client = _get_s3_client()
    if workers is None:
        workers = 2 * (os.cpu_count() or 4)

    if isinstance(multipart_threshold, str):
        multipart_threshold = utils.human_to_int(multipart_threshold)
    if isinstance(multipart_chunksize, str):
        multipart_chunksize = utils.human_to_int(multipart_chunksize)

    transfer = S3Transfer(
        client=client,
        config=TransferConfig(
            multipart_threshold=multipart_threshold,
            multipart_chunksize=multipart_chunksize,
            max_concurrency=max_concurrency,
            use_threads=True,
        ),
    )

    bucket, prefix = _parse_s3_uri(src)
    dst = Path(dst)
    paginator = client.get_paginator("list_objects_v2")

    total_size = 0
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                futures.append(
                    ex.submit(
                        _s3_download_file_helper,
                        bucket=bucket,
                        key=obj["Key"],
                        prefix=prefix,
                        dst=dst,
                        transfer=transfer,
                        expected_size=obj["Size"],
                    )
                )
    for fut in as_completed(futures):
        total_size += fut.result()
    return total_size


def _s3_download(src: str, dst: str | Path) -> None:
    if _s3_is_file(src):
        dst = Path(dst)
        bucket, key = _parse_s3_uri(src)
        _get_s3_client().download_file(bucket, key, str(dst))
    else:
        _s3_download_dir(src, dst)


def _s3_delete(path: str) -> None:
    if _s3_is_file(path):
        bucket, key = _parse_s3_uri(path)
        _get_s3_client().delete_object(Bucket=bucket, Key=key)
        return

    _s3_delete_many(_s3_list_objects(path))


def _s3_touch(path: str) -> None:
    bucket, key = _parse_s3_uri(path)
    _get_s3_client().put_object(Bucket=bucket, Key=key, Body=b"")


def _s3_list_objects(prefix: str) -> list[str]:
    client = _get_s3_client()
    bucket, key = _parse_s3_uri(prefix)
    if key and not key.endswith("/"):
        key += "/"

    paginator = client.get_paginator("list_objects_v2")
    uris = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for obj in page.get("Contents", []):
            uris.append(f"s3://{bucket}/{obj['Key']}")
    return uris


def _s3_delete_many(uris: list[str]) -> list[str]:
    if not uris:
        return []

    client = _get_s3_client()
    keys_by_bucket: dict[str, list[str]] = {}
    for uri in uris:
        bucket, key = _parse_s3_uri(uri)
        keys_by_bucket.setdefault(bucket, []).append(key)

    failed = []
    for bucket, keys in keys_by_bucket.items():
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i : i + 1000]]
            resp = client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            for error in resp.get("Errors", []):
                failed.append(f"s3://{bucket}/{error['Key']}")
    return failed


def _s3_listdir(path: str) -> list[str]:
    client = _get_s3_client()
    bucket, prefix = _parse_s3_uri(path)
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    paginator = client.get_paginator("list_objects_v2")
    children = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            name = common_prefix["Prefix"][len(prefix) :].rstrip("/")
            if name:
                children.append(name)
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if name:
                children.append(name)
    return children


# --- OCI ------------------------------------------------------------------

_OCI_URI_RE = re.compile(
    r"^oci://(?P<namespace>[a-zA-Z0-9_\-]+)(?:@(?P<region>[a-zA-Z0-9_\-]+))?/"
    r"(?P<bucket>[a-zA-Z0-9_\-]+)/(?P<key>.+)$"
)


def _parse_oci_uri(uri: str) -> tuple[str, str, str]:
    """Return ``(namespace, bucket, key)`` from an oci:// URI."""
    m = _OCI_URI_RE.match(uri)
    if not m:
        raise ValueError(f"Invalid OCI URI: {uri!r}. Expected oci://<namespace>/<bucket>/<key>")
    return m.group("namespace"), m.group("bucket"), m.group("key")


def _build_oci_client():
    """Build an OCI ObjectStorageClient, preferring config file over InstancePrincipals.

    Prefers ~/.oci/config when present (developer laptop path); falls back to
    InstancePrincipals when running on an OCI instance without a config file
    (CI, in-cluster).
    """
    try:
        import oci
    except ImportError as e:
        raise ImportError(
            "oci:// scheme requires the `oci` SDK on the submit machine. "
            "Install it with `pip install 'seekr-chain[oci]'` "
            "(or `pip install oci` directly)."
        ) from e

    config_path = Path(os.environ.get("OCI_CONFIG_FILE") or (Path.home() / ".oci/config"))
    if config_path.is_file():
        config = oci.config.from_file(file_location=str(config_path))
        return oci.object_storage.ObjectStorageClient(config)

    region = os.environ.get("OCI_REGION", "us-chicago-1")
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient({"region": region}, signer=signer)


def _get_oci_client():
    global _oci_client
    if _oci_client is None:
        _oci_client = _build_oci_client()
    return _oci_client


def _oci_exists(path: str) -> bool:
    """Return True iff the object exists. Swallows all exceptions as False.

    A 404 and an auth failure both look like "not present" to callers here.
    """
    namespace, bucket, key = _parse_oci_uri(path)
    client = _get_oci_client()
    try:
        client.head_object(namespace_name=namespace, bucket_name=bucket, object_name=key)
        return True
    except Exception:
        return False


def _oci_upload(src: str | Path, dst: str) -> None:
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"Source is not a file: {src}")
    namespace, bucket, key = _parse_oci_uri(dst)
    with open(src, "rb") as f:
        _get_oci_client().put_object(namespace_name=namespace, bucket_name=bucket, object_name=key, put_object_body=f)


def _oci_download(src: str, dst: str | Path) -> None:
    dst = Path(dst)
    namespace, bucket, key = _parse_oci_uri(src)
    client = _get_oci_client()
    try:
        resp = client.get_object(namespace_name=namespace, bucket_name=bucket, object_name=key)
    except Exception as e:
        if getattr(e, "status", None) == 404:
            listing = client.list_objects(namespace_name=namespace, bucket_name=bucket, prefix=key, limit=1)
            if listing.data.objects:
                raise NotImplementedError(
                    f"{src!r} is a directory/prefix, not a single object -- remote_fs only supports "
                    "downloading a single OCI object, not recursive directory transfer."
                ) from e
        raise

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        for chunk in resp.data.raw.stream(1024 * 1024, decode_content=False):
            f.write(chunk)
