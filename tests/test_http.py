"""The HTTP plumbing waits out a busy host a limited number of times and never longer than it should."""

from __future__ import annotations

import httpx
import pytest

import scout.http
from scout.http import RetryAfterTooLong, build_http_client


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the sleep between attempts, and record how long each one would have been."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(scout.http, "_sleep", record)
    return recorded


async def test_a_busy_host_is_retried_after_the_wait_it_asks_for_and_given_up_on_after_three_tries(
    waits: list[float],
) -> None:
    answers = iter(
        [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(answers)

    async with build_http_client(
        user_agent="scout-tests/0", timeout=5, transport=httpx.MockTransport(answer)
    ) as client:
        response = await client.get("https://example.test/")
    assert response.json() == {"ok": True}
    assert waits == [3.0, 2.0]  # the host's Retry-After, then 2 s on the second attempt without one
    assert len(sent) == 3

    waits.clear()
    sent.clear()

    def always_busy(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503)

    async with build_http_client(
        user_agent="scout-tests/0", timeout=5, transport=httpx.MockTransport(always_busy)
    ) as client:
        response = await client.get("https://example.test/")
    assert response.status_code == 503  # the last answer is returned for the caller to judge
    assert len(sent) == 3
    assert waits == [1.0, 2.0]


async def test_a_retry_after_over_the_limit_fails_at_once_without_waiting(waits: list[float]) -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(429, headers={"retry-after": "3600"})

    async with build_http_client(
        user_agent="scout-tests/0", timeout=5, transport=httpx.MockTransport(answer)
    ) as client:
        with pytest.raises(RetryAfterTooLong, match="Retry-After: 3600"):
            await client.get("https://example.test/")

    assert len(sent) == 1
    assert waits == []
