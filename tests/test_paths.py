"""SCOUT_HOME and the XDG variables decide where scout keeps its config and its data."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scout.paths import (
    check_profile_name,
    config_dir,
    credits_db_path,
    data_dir,
    global_config_path,
    profile_data_dir,
    profile_path,
    profiles_dir,
)


def test_scout_home_holds_the_config_and_the_data_and_expands_a_tilde(
    scout_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert config_dir() == scout_home
    assert global_config_path() == scout_home / "config.yaml"
    assert profiles_dir() == scout_home / "profiles"
    assert profile_path("default") == scout_home / "profiles" / "default.yaml"
    assert data_dir() == scout_home / "data"
    assert credits_db_path() == scout_home / "data" / "credits.db"  # one ledger for every profile
    assert profile_data_dir("default") == scout_home / "data" / "profiles" / "default"

    monkeypatch.setenv("SCOUT_HOME", "~/elsewhere")
    assert config_dir() == tmp_path / "user-home" / "elsewhere"


@pytest.mark.skipif(os.name == "nt", reason="Windows uses its own folders without these variables")
def test_xdg_variables_are_used_without_scout_home_and_the_users_home_is_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCOUT_HOME")
    user_home = tmp_path / "user-home"
    assert config_dir() == user_home / ".config" / "scout"
    assert data_dir() == user_home / ".local" / "share" / "scout"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert config_dir() == tmp_path / "xdg-config" / "scout"
    assert data_dir() == tmp_path / "xdg-data" / "scout"

    monkeypatch.setenv("SCOUT_HOME", str(tmp_path / "home"))  # SCOUT_HOME beats the XDG variables
    assert config_dir() == tmp_path / "home"
    assert data_dir() == tmp_path / "home" / "data"

    monkeypatch.setenv("SCOUT_HOME", "   ")  # and a blank one is ignored
    assert config_dir() == tmp_path / "xdg-config" / "scout"


def test_a_profile_name_cannot_walk_out_of_the_profiles_folder(scout_home: Path) -> None:
    for fine in ("default", "my-profile_2", "v1.2"):
        assert check_profile_name(fine) == fine
        assert profile_path(fine).parent == scout_home / "profiles"
    for bad in ("../evil", "a/b", "", ".hidden", "a..b", "x" * 65, "name with space"):
        with pytest.raises(ValueError, match="not a profile name"):
            profile_path(bad)
        with pytest.raises(ValueError):
            profile_data_dir(bad)


@pytest.mark.skipif(os.name == "nt", reason="Windows uses its own folders without these variables")
def test_a_relative_scout_home_is_made_absolute_and_a_relative_xdg_value_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCOUT_HOME", "relative/home")
    assert config_dir() == tmp_path / "relative" / "home"
    assert data_dir() == tmp_path / "relative" / "home" / "data"
    assert config_dir().is_absolute()

    monkeypatch.delenv("SCOUT_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    monkeypatch.setenv("XDG_DATA_HOME", "relative-data")
    user_home = tmp_path / "user-home"
    assert config_dir() == user_home / ".config" / "scout"  # the XDG spec: a relative value is invalid
    assert data_dir() == user_home / ".local" / "share" / "scout"
