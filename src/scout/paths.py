"""Where scout keeps its config and its data.

Config: ``$SCOUT_HOME``, else ``$XDG_CONFIG_HOME/scout``, else ``~/.config/scout`` (global ``config.yaml`` and
``profiles/<name>.yaml``). Data: ``$SCOUT_HOME/data``, else ``$XDG_DATA_HOME/scout``, else ``~/.local/share/scout``
(the credit ledger shared by every profile and ad-hoc call, and one folder per profile with its store and briefs).
On Windows, without those variables, the platform's own folders are used. A relative ``SCOUT_HOME`` is taken from the
current folder and made absolute; a relative ``XDG_*`` value is ignored, as the XDG Base Directory spec says.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import platformdirs

APP = "scout"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _env_path(name: str) -> Path | None:
    """The absolute folder an environment variable names: a relative one is made absolute from the current folder."""
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().absolute() if value else None


def _xdg_path(name: str) -> Path | None:
    """An ``XDG_*`` folder, or None when it is unset or relative (the spec says to ignore a relative one)."""
    value = os.environ.get(name, "").strip()
    path = Path(value).expanduser() if value else None
    return path if path is not None and path.is_absolute() else None


def config_dir() -> Path:
    home = _env_path("SCOUT_HOME")
    if home:
        return home
    xdg = _xdg_path("XDG_CONFIG_HOME")
    if xdg:
        return xdg / APP
    if os.name == "nt":
        return platformdirs.user_config_path(APP, appauthor=False)
    return Path.home() / ".config" / APP


def data_dir() -> Path:
    home = _env_path("SCOUT_HOME")
    if home:
        return home / "data"
    xdg = _xdg_path("XDG_DATA_HOME")
    if xdg:
        return xdg / APP
    if os.name == "nt":
        return platformdirs.user_data_path(APP, appauthor=False)
    return Path.home() / ".local" / "share" / APP


def global_config_path() -> Path:
    return config_dir() / "config.yaml"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def check_profile_name(name: str) -> str:
    """``name`` when it is a safe file name (letters, digits, ``_``, ``-``, ``.``), else ``ValueError``."""
    if not _PROFILE_NAME.match(name) or ".." in name:
        raise ValueError(f"{name!r} is not a profile name (letters, digits, _ - . only)")
    return name


def profile_path(name: str) -> Path:
    return profiles_dir() / f"{check_profile_name(name)}.yaml"


def credits_db_path() -> Path:
    """The credit ledger: one file for every profile and ad-hoc call, so the daily cap is global."""
    return data_dir() / "credits.db"


def profile_data_dir(name: str) -> Path:
    """A profile's store (``scout.db``), briefs and run manifests."""
    return data_dir() / "profiles" / check_profile_name(name)
