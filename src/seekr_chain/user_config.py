"""User/project configuration for seekr-chain.

The module-level ``config`` object is loaded once at import time by merging
all configuration sources in priority order:

1. Environment variables (highest priority)
2. ``.env`` file — walk up from CWD (local overrides, should be gitignored)
3. ``.seekrchain.toml`` — walk up from CWD (committed project default)
4. ``~/.seekrchain.toml`` — user home directory (personal global default)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

NixCompression = Literal["NONE", "ZSTD", "XZ", "BZIP2", "GZIP"]

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-reuse-imports]

import dotenv

_CONFIG_FILENAME = ".seekrchain.toml"

# Maps environment variable name -> UserConfig field name.
# Add new config values here; both .env and env var resolution use this map.
_ENV_VAR_MAP: dict[str, str] = {
    "SEEKRCHAIN_DATASTORE_ROOT": "datastore_root",
    "SEEKRCHAIN_INIT_IMAGE": "init_image",
    "SEEKRCHAIN_CONTROLLER_IMAGE": "controller_image",
    "SEEKRCHAIN_NIX_STORE": "nix_store",
    "SEEKRCHAIN_NIX_RUNNER_IMAGE": "nix_runner_image",
    "SEEKRCHAIN_NIX_STORE_VOLUME_KIND": "nix_store_volume_kind",
    "SEEKRCHAIN_NIX_STORE_HOSTPATH": "nix_store_hostpath",
    "SEEKRCHAIN_NIX_STORE_MAX_SIZE": "nix_store_max_size",
    "SEEKRCHAIN_NIX_COMPRESSION": "nix_compression",
}


@dataclass(frozen=True)
class UserConfig:
    """Process-wide configuration loaded from env vars + ``.seekrchain.toml``.

    Parameters
    ----------
    datastore_root :
        Base seekr-fs URI for workflow artifacts (assets, logs, metadata).
    init_image :
        OCI image for the chain-init container. Defaults to the pinned
        ``ghcr.io/seekr-technologies/seekr-chain-init`` build.
    nix_store :
        Default URI for the nix binary cache (e.g. ``s3://bucket``); any
        nix store type works. Used when a step's ``nix.store`` is not
        explicitly set; per-step overrides win.
    nix_runner_image :
        OCI image for nix-mode roles. Must contain ``nix`` + ``s5cmd`` with
        experimental-features + sandbox + filter-syscalls configured (see
        ``docker/Dockerfile.nix-runner`` for the reference build).
    nix_store_volume_kind :
        Volume kind for the in-pod ``/nix`` store. ``"hostPath"`` (default)
        shares the closure across pods on the same node (free per-node
        warm cache); requires the cluster's PodSecurity to admit hostPath
        volumes. ``"emptyDir"`` is per-pod — use when hostPath isn't
        allowed.
    nix_store_hostpath :
        Host filesystem path when ``nix_store_volume_kind == "hostPath"``.
        Defaults to ``/var/lib/seekr-chain/nix``; created on demand
        (DirectoryOrCreate), so no pre-provisioning DaemonSet is required.
    nix_store_max_size :
        Size budget for the hostPath store, parsed as bytes with optional
        IEC suffix (``"50GiB"``, ``"100G"``, etc.). When chain-nix-init
        finishes a pull, it checks store size; if above this limit, it
        deletes oldest closures (by atime) until under. Skips the closure
        we just fetched. Default: ``"50GiB"``. Set to a huge value (e.g.
        ``"1TiB"``) to effectively disable GC.
    nix_compression :
        Compression scheme for NARs the build step uploads. Affects only
        new uploads — existing paths keep whichever scheme they were written
        with (each narinfo records its own; consumers decode accordingly).
        Default ``"ZSTD"`` (fast, multi-threaded, good ratio). ``"NONE"``
        is faster end-to-end when storage is cheap and bandwidth is high
        (e.g. OCI Object Storage with ~10 GB/s links). ``"XZ"`` is small
        but ~5x slower and single-threaded; matches cache.nixos.org's
        historical format. ``"BZIP2"`` and ``"GZIP"`` are supported by nix
        but rarely chosen.
    """

    datastore_root: str | None = None
    init_image: str | None = None
    controller_image: str | None = None
    nix_store: str | None = None
    nix_runner_image: str | None = None
    nix_store_volume_kind: str | None = None
    nix_store_hostpath: str | None = None
    nix_store_max_size: str | None = None
    nix_compression: NixCompression | None = None


def _find_file_walking_up(filename: str) -> Path | None:
    current = Path.cwd()
    for directory in [current, *current.parents]:
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_config() -> UserConfig:
    """Load and merge all configuration sources, returning a UserConfig."""
    values: dict[str, str] = {}

    # 4. Global user config (~/.seekrchain.toml) — lowest priority, load first
    global_config = Path.home() / _CONFIG_FILENAME
    if global_config.is_file():
        values.update({k: v for k, v in _read_toml(global_config).items() if v is not None})

    # 3. Local project config (.seekrchain.toml, walk up from CWD)
    local_config = _find_file_walking_up(_CONFIG_FILENAME)
    if local_config:
        values.update({k: v for k, v in _read_toml(local_config).items() if v is not None})

    # 2. .env file (walk up from CWD)
    dotenv_path = dotenv.find_dotenv(usecwd=True)
    if dotenv_path:
        env_values = dotenv.dotenv_values(dotenv_path)
        for env_var, field in _ENV_VAR_MAP.items():
            if value := env_values.get(env_var):
                values[field] = value

    # 1. Environment variables (highest priority)
    for env_var, field in _ENV_VAR_MAP.items():
        if value := os.environ.get(env_var):
            values[field] = value

    return UserConfig(
        datastore_root=values.get("datastore_root"),
        init_image=values.get("init_image"),
        controller_image=values.get("controller_image"),
        nix_store=values.get("nix_store"),
        nix_runner_image=values.get("nix_runner_image"),
        nix_store_volume_kind=values.get("nix_store_volume_kind"),
        nix_store_hostpath=values.get("nix_store_hostpath"),
        nix_store_max_size=values.get("nix_store_max_size"),
        nix_compression=values.get("nix_compression"),
    )


config: UserConfig = _load_config()
