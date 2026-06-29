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
    "SEEKRCHAIN_NIX_STORE": "nix_store",
    "SEEKRCHAIN_NIX_RUNNER_IMAGE": "nix_runner_image",
    "SEEKRCHAIN_NIX_STORE_VOLUME_KIND": "nix_store_volume_kind",
    "SEEKRCHAIN_NIX_STORE_HOSTPATH": "nix_store_hostpath",
    "SEEKRCHAIN_NIX_COMPRESSION": "nix_compression",
}


@dataclass(frozen=True)
class UserConfig:
    datastore_root: str | None = None
    init_image: str | None = None
    # Default seekr-fs URI for the nix binary cache (e.g. s3://bucket/nix-cache).
    # Used when a step's `nix.store` is not explicitly set. Per-step overrides win.
    nix_store: str | None = None
    # OCI image for nix-mode roles. Must contain `nix` + `s5cmd` and have
    # experimental-features + sandbox + filter-syscalls configured (see
    # nix_poc/Dockerfile.nix-runner for the reference build).
    nix_runner_image: str | None = None
    # Volume kind for the in-pod /nix store:
    #   "hostPath"  — share across pods on the same node. Closures fetched by
    #                 earlier pods are reused by later pods landing on the
    #                 same node (free per-node cache). Default. Requires the
    #                 cluster's PodSecurity to admit hostPath volumes.
    #   "emptyDir"  — per-pod. Closure pull repeats on every pod. Use when
    #                 hostPath isn't allowed by the cluster.
    nix_store_volume_kind: str | None = None  # defaults to "hostPath" at use site
    # Host path used when nix_store_volume_kind == "hostPath". Created on
    # demand (DirectoryOrCreate) — no pre-provisioning DaemonSet required.
    nix_store_hostpath: str | None = None  # defaults to /var/lib/seekr-chain/nix
    # Compression scheme for NARs uploaded to nix_store. Affects only new
    # uploads — existing paths in the cache keep whichever compression they
    # were written with (each narinfo records its own scheme; consumers
    # decode accordingly).
    #   "ZSTD"  — fast, multi-threaded; good default. (Current default.)
    #   "NONE"  — no compression. Use when storage is cheap + bandwidth is
    #             high (e.g. OCI Object Storage with ~10 GB/s links). Saves
    #             producer + consumer CPU at the cost of storage size.
    #   "XZ"    — small, slow, single-threaded. nix's historical default;
    #             matches cache.nixos.org's format.
    #   "BZIP2", "GZIP" — supported by nix but rarely chosen.
    nix_compression: NixCompression | None = None  # defaults to "ZSTD" at use site


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
        nix_store=values.get("nix_store"),
        nix_runner_image=values.get("nix_runner_image"),
        nix_store_volume_kind=values.get("nix_store_volume_kind"),
        nix_store_hostpath=values.get("nix_store_hostpath"),
        nix_compression=values.get("nix_compression"),
    )


config: UserConfig = _load_config()
