"""Constants and fakes the tests share (the fixtures that install them are in ``conftest.py``)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scout.models import Item

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-key-not-real"
USER_AGENT = "scout-tests/0"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DAY = "2026-10-20"
SC_HOST = "api.scrapecreators.com"
BALANCE_PATH = "/v1/credit-balance"


class NetworkBlocked(AssertionError):
    """A test tried to use the real network, or to start a real program."""


def blocked(what: str) -> NetworkBlocked:
    return NetworkBlocked(f"a test used the real network ({what}); drive clients through httpx.MockTransport")


class FakeKeychain:
    """Stands in for the ``security`` command: records its calls and answers "not found" unless told otherwise."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.returncode = 44
        self.stdout = ""
        self.error: BaseException | None = None

    def find(self, value: str) -> None:
        self.returncode, self.stdout = 0, value + "\n"

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


class FakeWeb:
    """Answers every HTTP request of scout's clients from a table, and remembers each one.

    ``on(host, path_prefix, body=...)`` routes a host and a path prefix (the longest prefix wins, a later route wins a
    tie). The free ``/v1/credit-balance`` answer is there from the start. A request nobody routed fails the test.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.transport = httpx.MockTransport(self._handle)
        self.on(SC_HOST, BALANCE_PATH, body={"creditCount": 1000})

    def on(
        self,
        host: str,
        path: str,
        *,
        body: Any = None,
        text: str | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> None:
        if handler is None:

            def handler(request: httpx.Request) -> httpx.Response:
                if text is not None:
                    return httpx.Response(status, text=text, headers=headers)
                if body is not None:
                    return httpx.Response(status, json=body, headers=headers)
                return httpx.Response(status, headers=headers)

        self._routes.append((host, path, handler))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        matches = [
            (len(prefix), index, handler)
            for index, (host, prefix, handler) in enumerate(self._routes)
            if request.url.host == host and request.url.path.startswith(prefix)
        ]
        if not matches:
            raise AssertionError(f"a test sent a request nobody routed: {request.method} {request.url}")
        return max(matches, key=lambda m: m[:2])[2](request)

    def sent_to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    @property
    def paid(self) -> list[httpx.Request]:
        """The requests that can cost credits: everything sent to ScrapeCreators except the free balance check."""
        return [r for r in self.sent_to(SC_HOST) if r.url.path != BALANCE_PATH]


ITEM_KEYS = {
    "source",
    "id",
    "key",
    "url",
    "title",
    "text",
    "author",
    "community",
    "created_at",
    "score",
    "comments",
    "views",
    "upvote_ratio",
    "link",
    "extra",
}


def check_shape(item: Item, source: str) -> dict[str, Any]:
    """Assert ``item`` has the one documented shape every source normalises to; return it as a dict."""
    data = item.to_dict()
    assert set(data) == ITEM_KEYS
    assert data["source"] == source
    assert data["id"] and data["key"] == f"{source}:{data['id']}"
    assert data["url"].startswith("https://")
    assert datetime.fromisoformat(data["created_at"]).tzinfo is not None
    assert isinstance(data["score"], int) and isinstance(data["comments"], int)
    assert isinstance(data["title"], str) and isinstance(data["text"], str) and isinstance(data["author"], str)
    json.dumps(data)  # JSON-serialisable
    return data
