"""Opt-in, shareable closure-path memo file (not a Nix binary cache)."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

_STORE_PATH = re.compile(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+._?=-]+")


def source_exclusions(cache: Path, source: str | Path) -> list[str]:
    """Keep the memo and its writer artifacts out of Nix input trees."""
    try:
        relative = cache.relative_to(Path(source).resolve()).as_posix()
    except ValueError:
        return []
    return [f"/{relative}", f"/{relative}.lock", f"/{relative}.tmp-*"]


def _read(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), dict):
            raise ValueError("expected version 1 with an entries object")
        for key, entry in data["entries"].items():
            if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(entry, dict):
                raise ValueError("invalid cache entry")
            if not isinstance(entry.get("closure_path"), str) or not _STORE_PATH.fullmatch(entry["closure_path"]):
                raise ValueError("invalid closure path")
        return data
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid Nix closure cache {path}: {exc}") from exc


def lookup(path: Path, key: str) -> str | None:
    entry = _read(path)["entries"].get(key)
    return entry["closure_path"] if entry else None


def store(path: Path, key: str, *, source_digest: str, expression: str, attr: str, system: str, closure: str) -> None:
    """Merge under a stable sidecar lock, then atomically publish sorted JSON."""
    if not _STORE_PATH.fullmatch(closure):
        raise ValueError(f"Invalid Nix closure path: {closure!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        entry = dict(source_digest=source_digest, expression=expression, attr=attr, system=system, closure_path=closure)
        if data["entries"].get(key) == entry:
            return
        data["entries"][key] = entry
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=path.name + ".tmp-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
