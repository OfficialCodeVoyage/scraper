"""Fixtures shared by every test.

Four things are on for every test without being asked for:

* no real network: a socket, an httpx transport or a subprocess that is really used fails the test;
* ``SCOUT_HOME`` (and ``HOME``) point into ``tmp_path``, so nothing touches ``~/.config/scout`` or
  ``~/.local/share/scout``;
* no key: ``SCRAPECREATORS_API_KEY`` is removed and the Keychain command is a fake that finds nothing;
* the clock the API uses is fixed at ``NOW`` (``DAY`` is its calendar day).

A test that needs HTTP asks for ``web``, which answers every request of scout's clients from a table and remembers it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import types
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from support import FAKE_KEY, FIXTURES, NOW, USER_AGENT, FakeKeychain, FakeWeb, blocked

import scout.api
import scout.http
import scout.keys
from scout.models import Item
from scout.scrapecreators import ScrapeCreatorsClient
from scout.store import CreditLedger


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> None:
        if self.family == getattr(socket, "AF_UNIX", None):
            return real_connect(self, address)
        raise blocked(f"socket connect to {address!r}")

    def connect_ex(self: socket.socket, address: Any) -> int:
        if self.family == getattr(socket, "AF_UNIX", None):
            return real_connect_ex(self, address)
        raise blocked(f"socket connect to {address!r}")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise blocked(f"{args!r}")

    async def refuse_async(*args: Any, **kwargs: Any) -> Any:
        raise blocked(f"{args!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)


@pytest.fixture(autouse=True)
def scout_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """SCOUT_HOME inside tmp_path; HOME too, so even a fallback to ``~`` stays in the sandbox."""
    home = tmp_path / "scout-home"
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("SCOUT_HOME", str(home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", scout.keys.KEY_NAME):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(
        scout.keys, "subprocess", types.SimpleNamespace(run=fake.run, SubprocessError=subprocess.SubprocessError)
    )
    # Pretend to be macOS so the Keychain path runs on every OS; a test sets another platform to prove it is skipped.
    monkeypatch.setattr(scout.keys, "sys", types.SimpleNamespace(platform="darwin"))
    return fake


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scout.api, "local_now", lambda config=None: NOW)


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(scout.keys.KEY_NAME, FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    """The fake web, installed under every client scout builds for itself (``api.*``, the CLI)."""
    fake = FakeWeb()
    monkeypatch.setattr(scout.http.httpx, "AsyncHTTPTransport", lambda *args, **kwargs: fake.transport)
    return fake


@pytest.fixture
def load() -> Callable[[str], Any]:
    """``load("scrapecreators/reddit_search")``: the parsed JSON of a file under ``tests/fixtures``."""

    def _load(name: str) -> Any:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def ledger(scout_home: Path) -> Iterator[CreditLedger]:
    """The default ledger (``credits.db`` under SCOUT_HOME), opened on a connection of its own, for reading."""
    opened = CreditLedger.default()
    yield opened
    opened.close()


@pytest.fixture
async def make_client(web: FakeWeb, scout_home: Path) -> AsyncIterator[Callable[..., ScrapeCreatorsClient]]:
    """``make_client(cap)``: a ScrapeCreators client on the fake web and, unless given one, its own ledger connection."""
    clients: list[ScrapeCreatorsClient] = []
    ledgers: list[CreditLedger] = []

    def make(
        cap: int, *, day: str = "2026-10-20", key: str = FAKE_KEY, ledger: CreditLedger | None = None
    ) -> ScrapeCreatorsClient:
        if ledger is None:
            ledger = CreditLedger.default()
            ledgers.append(ledger)
        client = ScrapeCreatorsClient(key, ledger, cap, day=day, user_agent=USER_AGENT, transport=web.transport)
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()
    for opened in ledgers:
        opened.close()


@pytest.fixture
def set_cap(scout_home: Path) -> Callable[[int], None]:
    def _set(cap: int) -> None:
        scout_home.mkdir(parents=True, exist_ok=True)
        (scout_home / "config.yaml").write_text(f"daily_credit_cap: {cap}\n", encoding="utf-8")

    return _set


@pytest.fixture
def add_profile(scout_home: Path) -> Callable[[str, str], Path]:
    def _add(name: str, text: str) -> Path:
        folder = scout_home / "profiles"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _add


@pytest.fixture
def make_item() -> Callable[..., Item]:
    """``make_item("p1", score=50)``: a Reddit item two hours old; any field can be overridden."""

    def make(ext_id: str = "p1", **fields: Any) -> Item:
        values: dict[str, Any] = {
            "source": "reddit",
            "ext_id": ext_id,
            "url": f"https://example.com/{ext_id}",
            "title": f"Title of {ext_id}",
            "text": "",
            "author": "fake_author",
            "community": "ExampleSub",
            "created_at": NOW - timedelta(hours=2),
            "score": 100,
            "comments": 10,
            "lens": "video_ideas",
            "watch": "r/ExampleSub",
        }
        values.update(fields)
        return Item(**values)

    return make
