"""Default locations on the box (pi-ops layout, docs/DEPLOY.md).

The box runs under pi-ops' git-deploy as the Pi's service user, from the clone
in ~/apps/daily-panel. Nothing is written inside the clone at runtime:

    ~/.config/zozobox/box.toml     static box config  ($ZOZOBOX_CONFIG wins)
    ~/.config/zozobox/env          secrets (pi-ops env_file, mode 600)
    ~/.local/share/zozobox/        all data: SQLite store, media, chimes, locks
                                   ($ZOZOBOX_DATA_DIR wins)

Computed from Path.home() at call time (systemd Environment= does not expand
`~`, and HOME is the service user's home under systemd's User=).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

CONFIG_ENV = "ZOZOBOX_CONFIG"
DATA_DIR_ENV = "ZOZOBOX_DATA_DIR"


def default_config_dir() -> Path:
    return Path.home() / ".config" / "zozobox"


def default_config_path() -> Path:
    return default_config_dir() / "box.toml"


def default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "zozobox"


def data_dir_from_env(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env[DATA_DIR_ENV]) if env.get(DATA_DIR_ENV) else default_data_dir()


def config_path_from_env(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env[CONFIG_ENV]) if env.get(CONFIG_ENV) else default_config_path()


__all__ = ["CONFIG_ENV", "DATA_DIR_ENV", "config_path_from_env", "data_dir_from_env",
           "default_config_dir", "default_config_path", "default_data_dir"]
