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
}


@dataclass(frozen=True)
class UserConfig:
    datastore_root: str | None = None


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
    )


config: UserConfig = _load_config()
