"""Helpers for resolving nix expressions and checking binary cache contents.

The submit path uses these to:

- Turn a user-supplied nix expression + attribute into a concrete
  ``/nix/store/<hash>-<name>`` closure path (``eval_closure_path``).
- Check whether that closure already exists in the configured binary
  cache (``closure_exists``).

Evaluation requires ``nix`` on the local PATH. Evaluation is pure (no
compilation, no system-specific code execution) so a Mac can resolve
the closure path for an ``x86_64-linux`` expression — only the
*realization* (building) needs to happen on the target system, which
seekr-chain hands off to an in-cluster build step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from seekr_chain import constants, remote_fs
from seekr_chain.symlink import copy_filtered, fingerprint_filtered_tree

logger = logging.getLogger(__name__)


class NixNotInstalledError(RuntimeError):
    """Raised when ``nix`` is required on the submit machine but isn't on PATH.

    Install from https://nixos.org/download.
    """


class NixEvalError(RuntimeError):
    """Raised when ``nix eval`` exits non-zero (syntax error, missing attr, etc.)."""


# A hung substituter or DNS blackhole would otherwise block `chain submit`
# forever; matches the 10-minute AWS_REQUEST_TIMEOUT convention nix-build.sh
# uses for the same "multi-GB / cold-cache fetch can legitimately take a
# while" reasoning.
_NIX_EVAL_TIMEOUT_S = int(os.environ.get("SEEKR_CHAIN_NIX_EVAL_TIMEOUT_S", "600"))
_NIX_CLOSURE_CACHE_ROOT = constants.LOCAL_CACHE / "nix" / "closure-cache" / "v1"


@dataclass(frozen=True)
class MaybeEvalClosureResult:
    closure_path: str
    expression_rel: str
    source_digest: str | None = None
    source_subdir: str | None = None
    staged_source_dir: Path | None = None
    # Default True: with no store_uri there's no remote store to check, and
    # callers that never asked for one shouldn't be told to schedule a build.
    closure_in_store: bool = True


def is_nix_installed() -> bool:
    """Return True iff ``nix`` is on the local PATH."""
    return shutil.which("nix") is not None


def closure_hash_from_path(closure_path: str) -> str:
    """Extract the content-addressed hash from a ``/nix/store/<hash>-<name>`` path.

    The hash is the leading component of the basename (everything before
    the first ``-``). It's the same hash that names the ``<hash>.narinfo``
    in a binary cache, which is what we look up to test existence.
    """
    if not closure_path.startswith("/nix/store/"):
        raise ValueError(f"expected absolute /nix/store path, got {closure_path!r}")
    basename = closure_path.removeprefix("/nix/store/")
    hash_part, _, _ = basename.partition("-")
    if not hash_part:
        raise ValueError(f"could not extract hash from {closure_path!r} (expected /nix/store/<hash>-<name>)")
    return hash_part


def eval_closure_path(expression: str, attr: str = "default", system: str = "x86_64-linux") -> str:
    """Evaluate ``<expression>#<attr>.outPath`` and return the closure store path.

    Parameters
    ----------
    expression
        Path to a ``.nix`` file or to a directory containing ``flake.nix``.
        Relative paths are resolved against the current working directory.
    attr
        Attribute path within the expression. Defaults to ``"default"``,
        which for a flake means ``packages.<system>.default``. For a flake
        the ``--system`` argument selects the system entry.
    system
        Target system (default ``x86_64-linux``). Eval is pure, so this
        works cross-system on a Mac; only realization needs to match.

    Raises
    ------
    NixNotInstalledError
        If ``nix`` isn't on PATH.
    NixEvalError
        If ``nix eval`` exits non-zero. The stderr is included in the
        exception message — usually a syntax error or missing attribute.
    """
    if not is_nix_installed():
        raise NixNotInstalledError(
            "nix is required on the submit machine to evaluate `nix.expression`. "
            "Install from https://nixos.org/download."
        )

    expr_path = Path(expression).resolve()
    if not expr_path.exists():
        raise FileNotFoundError(f"nix expression path does not exist: {expr_path}")

    # Both flake.nix and plain .nix are supported.
    # - Flake (dir with flake.nix OR `.#attr` syntax):  use `nix eval` with flake ref.
    # - Plain .nix file:  pass as a path expression `import <file>`-style.
    if expr_path.is_dir() and (expr_path / "flake.nix").exists():
        target = f"path:{expr_path}#packages.{system}.{attr}.outPath"
    elif expr_path.suffix == ".nix" and expr_path.is_file():
        # For a classic .nix file, evaluate the attr on the imported expression.
        # We can't use the flake ref syntax; use --expr to wrap.
        target_expr = (
            f"((import {expr_path}) {{}}).{attr}.outPath"
            if attr != "default"
            else f"((import {expr_path}) {{}}).outPath"
        )
        # nix eval --raw --impure --expr '...'
        cmd = ["nix", "eval", "--raw", "--impure", "--expr", target_expr]
        return _run_nix_eval(cmd, expression, attr)
    else:
        raise ValueError(
            f"nix.expression must point to a .nix file or a directory containing flake.nix; got {expr_path}"
        )

    cmd = ["nix", "eval", "--raw", target]
    return _run_nix_eval(cmd, expression, attr)


def materialize_nix_source_tree(
    src: str | Path,
    staging_dir: str | Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[str, Path]:
    """Materialize the filtered nix source set into this submit's staging dir.

    Returns ``(digest, workspace_path)`` where ``workspace_path`` is the copied
    real-file tree under ``<staging_dir>/nix-workspaces/<digest>/workspace`` —
    already the location asset packaging tars up, so no separate linking step
    is needed.
    """
    digest = fingerprint_filtered_tree(src, include=include, exclude=exclude)
    workspace = Path(staging_dir) / "nix-workspaces" / digest / "workspace"
    if workspace.is_dir():
        return digest, workspace

    try:
        copy_filtered(src, workspace, include=include, exclude=exclude)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return digest, workspace


def fingerprint_nix_source_tree(
    src: str | Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> str:
    """Return the content digest for the filtered nix source set."""
    return fingerprint_filtered_tree(src, include=include, exclude=exclude)


def _closure_in_store(store_uri: str | None, closure_path: str) -> bool:
    """True if the closure needs no build, i.e. there's nothing to check or it's already there."""
    if store_uri is None:
        return True
    return closure_exists(store_uri, closure_path)


def maybe_eval_closure(
    *,
    code_path: str,
    staged_root: str,
    staging_dir: str | Path,
    resolved_expression: str,
    role_name: str,
    expression: str,
    attr: str = "default",
    system: str = "x86_64-linux",
    source_include: list[str] | None = None,
    source_exclude: list[str] | None = None,
    store_uri: str | None = None,
) -> MaybeEvalClosureResult:
    """Resolve a closure path, using local/remote memo stores when available.

    When ``source_include`` or ``source_exclude`` is provided, the closure is
    keyed by the digest of that filtered nix source set. A cache miss
    materializes the copied source tree into ``staging_dir``, evals there,
    and writes both local and best-effort remote memo entries. A remote hit
    backfills the local cache.
    """
    expression_rel = os.path.relpath(resolved_expression, os.path.normpath(code_path))
    separate_nix_source = source_include is not None or source_exclude is not None
    if not separate_nix_source:
        staged_expression = os.path.normpath(os.path.join(staged_root, expression_rel))
        closure = eval_closure_path(staged_expression, attr=attr, system=system)
        return MaybeEvalClosureResult(
            closure_path=closure,
            expression_rel=expression_rel,
            closure_in_store=_closure_in_store(store_uri, closure),
        )

    source_digest = fingerprint_nix_source_tree(
        code_path,
        include=source_include,
        exclude=source_exclude,
    )
    closure = lookup_cached_closure_path(
        source_digest,
        expression_rel,
        attr=attr,
        system=system,
        store_uri=store_uri,
    )
    source_subdir = f"nix-workspaces/{source_digest}/workspace"
    if closure is not None:
        closure_in_store = _closure_in_store(store_uri, closure)
        staged_source_dir = None
        if not closure_in_store:
            _digest, staged_source_dir = materialize_nix_source_tree(
                code_path,
                staging_dir,
                include=source_include,
                exclude=source_exclude,
            )
        return MaybeEvalClosureResult(
            closure_path=closure,
            expression_rel=expression_rel,
            source_digest=source_digest,
            source_subdir=source_subdir,
            staged_source_dir=staged_source_dir,
            closure_in_store=closure_in_store,
        )

    # Cache miss — no cross-process lock: the materialize target is private
    # to this submit's staging_dir, and duplicate materialize+eval work
    # across racing submits is cheap (~10s) and tolerated rather than
    # coordinated against.
    _digest, source_root = materialize_nix_source_tree(
        code_path,
        staging_dir,
        include=source_include,
        exclude=source_exclude,
    )
    staged_expression = os.path.normpath(os.path.join(source_root, expression_rel))
    if not os.path.exists(staged_expression):
        raise ValueError(
            f"role {role_name!r}: nix.expression={expression!r} is not present in the staged nix source tree. "
            "Expand nix.include / nix.exclude so the flake and its inputs are copied for submit-time eval and "
            "in-cluster builds."
        )
    closure = eval_closure_path(staged_expression, attr=attr, system=system)
    store_cached_closure_path(
        source_digest,
        expression_rel,
        closure,
        attr=attr,
        system=system,
        store_uri=store_uri,
    )
    closure_in_store = _closure_in_store(store_uri, closure)
    staged_source_dir = source_root
    if closure_in_store:
        # Already in the store — the copy we just made won't be uploaded
        # for a build, so don't leave it sitting in the staging dir.
        shutil.rmtree(source_root, ignore_errors=True)
        staged_source_dir = None
    return MaybeEvalClosureResult(
        closure_path=closure,
        expression_rel=expression_rel,
        source_digest=source_digest,
        source_subdir=source_subdir,
        staged_source_dir=staged_source_dir,
        closure_in_store=closure_in_store,
    )


def lookup_cached_closure_path(
    source_digest: str,
    expression: str,
    attr: str = "default",
    system: str = "x86_64-linux",
    store_uri: str | None = None,
) -> str | None:
    key_hash = _closure_memo_key_hash(source_digest, expression, attr, system)
    closure = _read_local_closure_memo(key_hash)
    if closure is not None:
        logger.info(
            "Reusing cached nix closure from local memo for source=%s expression=%r", source_digest[:12], expression
        )
        return closure

    if store_uri is None:
        return None

    closure = _read_remote_closure_memo(store_uri, key_hash)
    if closure is None:
        return None
    logger.info(
        "Reusing cached nix closure from remote memo for source=%s expression=%r", source_digest[:12], expression
    )
    _write_local_closure_memo(key_hash, closure)
    return closure


def store_cached_closure_path(
    source_digest: str,
    expression: str,
    closure_path: str,
    attr: str = "default",
    system: str = "x86_64-linux",
    store_uri: str | None = None,
) -> None:
    if not closure_path.startswith("/nix/store/"):
        raise ValueError(f"expected /nix/store closure path, got {closure_path!r}")
    key_hash = _closure_memo_key_hash(source_digest, expression, attr, system)
    _write_local_closure_memo(key_hash, closure_path)
    if store_uri is not None:
        _write_remote_closure_memo(store_uri, key_hash, closure_path)


def _closure_memo_key_hash(source_digest: str, expression: str, attr: str, system: str) -> str:
    payload = json.dumps(
        {
            "source": source_digest,
            "expression": os.path.normpath(expression),
            "attr": attr,
            "system": system,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _local_closure_memo_path(key_hash: str) -> Path:
    return _NIX_CLOSURE_CACHE_ROOT / key_hash[:2] / f"{key_hash[2:]}.txt"


def _closure_memo_uri(store_uri: str, key_hash: str) -> str:
    base = urlsplit(store_uri.rstrip("/"))._replace(query="", fragment="")
    return f"{urlunsplit(base)}/closure-cache/v1/{key_hash[:2]}/{key_hash[2:]}.txt"


def _read_local_closure_memo(key_hash: str) -> str | None:
    path = _local_closure_memo_path(key_hash)
    if not path.is_file():
        return None
    try:
        return _validate_closure_memo_value(path.read_text().strip())
    except OSError as e:
        logger.warning("could not read nix closure memo %s: %s; ignoring it", path, e)
        return None


def _write_local_closure_memo(key_hash: str, closure_path: str) -> None:
    path = _local_closure_memo_path(key_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        tmp.write(closure_path)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _read_remote_closure_memo(store_uri: str, key_hash: str) -> str | None:
    uri = _closure_memo_uri(store_uri, key_hash)
    try:
        if uri.startswith("s3://"):
            import boto3

            s3 = boto3.client("s3")
            bucket, key = remote_fs.parse_uri(uri)
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode().strip()
            return _validate_closure_memo_value(body)
        if uri.startswith("oci://"):
            namespace, bucket, key = _parse_oci_uri(uri)
            client = _default_oci_client()
            body = client.get_object(namespace_name=namespace, bucket_name=bucket, object_name=key).data.content
            return _validate_closure_memo_value(body.decode().strip())
    except Exception:
        return None
    logger.debug("remote nix closure memo unsupported for store=%r", store_uri)
    return None


def _write_remote_closure_memo(store_uri: str, key_hash: str, closure_path: str) -> None:
    uri = _closure_memo_uri(store_uri, key_hash)
    try:
        if uri.startswith("s3://"):
            import boto3

            s3 = boto3.client("s3")
            bucket, key = remote_fs.parse_uri(uri)
            s3.put_object(Bucket=bucket, Key=key, Body=closure_path.encode(), ContentType="text/plain")
            return
        if uri.startswith("oci://"):
            namespace, bucket, key = _parse_oci_uri(uri)
            client = _default_oci_client()
            client.put_object(
                namespace_name=namespace,
                bucket_name=bucket,
                object_name=key,
                put_object_body=closure_path.encode(),
                content_type="text/plain",
            )
            return
    except Exception as e:
        logger.warning("could not write nix closure memo to %s: %s; continuing without remote backfill", uri, e)
        return
    logger.debug("remote nix closure memo unsupported for store=%r", store_uri)


def _validate_closure_memo_value(value: str) -> str | None:
    if value.startswith("/nix/store/"):
        return value
    return None


def _run_nix_eval(cmd: list[str], expression: str, attr: str) -> str:
    """Run `nix eval`, returning the closure path on stdout.

    Stderr is passed through to the parent's terminal so the user sees
    nix's download / build progress (which can take minutes on a cold
    nixpkgs unstable fetch). Without this, ``chain submit`` looks hung
    while nix silently downloads ~500 MB. Stderr isn't captured for
    the error message — if eval fails, the user has already seen the
    error scroll past on stderr.
    """
    logger.info(
        "Evaluating nix expression %r (attr=%r) — `nix eval` output follows",
        expression,
        attr,
    )
    logger.debug("running nix eval: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            text=True,
            check=False,
            timeout=_NIX_EVAL_TIMEOUT_S,
            # stderr=None: inherit parent's stderr so progress prints live.
        )
    except FileNotFoundError as e:
        # PATH-changed-after-import case.
        raise NixNotInstalledError("nix binary not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise NixEvalError(
            f"`nix eval` timed out after {_NIX_EVAL_TIMEOUT_S}s for expression={expression!r} "
            f"attr={attr!r}; check network connectivity/flake inputs, or raise "
            "SEEKR_CHAIN_NIX_EVAL_TIMEOUT_S."
        ) from e

    if result.returncode != 0:
        raise NixEvalError(
            f"`nix eval` failed for expression={expression!r} attr={attr!r} "
            f"(exit {result.returncode}); see error output above."
        )

    out = result.stdout.strip()
    if not out.startswith("/nix/store/"):
        raise NixEvalError(f"nix eval returned an unexpected output (expected /nix/store/...): {out!r}")
    logger.info("Resolved closure path: %s", out)
    return out


# Minimal OCI URI grammar for the closure memo's remote read/write. Full URI
# parsing (regions, glob, delimiter listing, etc.) lives in remote_fs; we
# only need namespace / bucket / key here.
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


def _default_oci_client():
    """Build an OCI ObjectStorageClient, preferring config file over InstancePrincipals.

    Prefers ~/.oci/config when present (developer laptop path); falls back to
    InstancePrincipals when running on an OCI instance without a config
    file (CI, in-cluster). The import guard lives here (not in the caller)
    so tests can monkeypatch _default_oci_client with a fake and bypass the
    SDK entirely.
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


def closure_exists(store_uri: str, closure_path: str) -> bool:
    """Return True iff the closure's narinfo exists at the configured store.

    Looks up ``{store_uri}/{hash}.narinfo`` in the store:

    - ``s3://``/``oci://`` route through ``remote_fs``.
    - Other schemes (``azure://``, ``gs://`` …) route through seekr-fs,
      imported lazily — those users need to ``pip install seekr-fs``.
    """
    started = time.perf_counter()
    hash_ = closure_hash_from_path(closure_path)
    # Query params on store_uri (endpoint=, scheme=, region=, ...) configure
    # nix's own S3 client, not boto3 -- boto3 gets its endpoint/region from
    # the environment/AWS config instead. Drop them before appending the
    # narinfo key, or they'd land inside remote_fs's bucket-name match.
    base = urlsplit(store_uri.rstrip("/"))._replace(query="", fragment="")
    narinfo_uri = f"{urlunsplit(base)}/{hash_}.narinfo"

    if narinfo_uri.startswith("s3://") or narinfo_uri.startswith("oci://"):
        exists = remote_fs.exists(narinfo_uri)
        logger.info(
            "Checked nix closure in store %s via remote_fs in %.3fs (exists=%s)",
            narinfo_uri,
            time.perf_counter() - started,
            exists,
        )
        return exists

    try:
        import seekr_fs as sfs
    except ImportError as e:
        raise ImportError(
            f"nix.store={store_uri!r} uses a non-s3/non-oci scheme; seekr-fs is "
            "required for that. Install it with `pip install seekr-fs` (or any "
            "compatible internal source)."
        ) from e
    exists = sfs.exists(narinfo_uri)
    logger.info(
        "Checked nix closure in store %s via generic backend in %.3fs (exists=%s)",
        narinfo_uri,
        time.perf_counter() - started,
        exists,
    )
    return exists
