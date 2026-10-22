"""The guard that keeps this suite offline works, and every test runs inside a throwaway SCOUT_HOME."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from scout.paths import config_dir, credits_db_path, data_dir


def test_a_real_http_request_fails_the_test_instead_of_leaving_the_machine() -> None:
    with pytest.raises(AssertionError, match="real network"):
        httpx.get("https://example.com/")


async def test_a_real_async_http_request_and_a_raw_socket_fail_the_test() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="real network"):
            await client.get("https://example.com/")
    with pytest.raises(AssertionError, match="real network"):
        socket.create_connection(("example.com", 443), timeout=1)
    with socket.socket() as raw, pytest.raises(AssertionError, match="real network"):
        raw.connect(("192.0.2.1", 443))


def test_every_test_uses_a_scout_home_inside_tmp_path_not_the_users_folders(tmp_path: Path) -> None:
    for folder in (config_dir(), data_dir(), credits_db_path()):
        assert tmp_path in folder.parents
    assert tmp_path in Path.home().parents
