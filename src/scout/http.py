"""HTTP plumbing: a strict sliding-window rate limiter, a client that waits out 429/503, and error text.

The limiter sits in the transport, below the retry logic, so a retried request counts as a request of its own. It is
a strict sliding window, not a leaky bucket: a leaky bucket lets a full burst through and up to twice the rate can
pass across a window boundary, which would break a hard cap such as GitHub's 10 anonymous searches a minute.
A host that answers 429 or 503 is asking for a pause: up to ``MAX_ATTEMPTS`` tries per request, waiting the host's
``Retry-After``, or 1 s then 2 s without one. A ``Retry-After`` above ``MAX_RETRY_WAIT`` fails the request at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 503})
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 10.0
MAX_ERROR_TEXT = 300


class RateLimiter:
    """At most ``max_requests`` in any sliding window of ``per_seconds``, shared by all callers."""

    def __init__(
        self,
        max_requests: int,
        per_seconds: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self._max_requests = max_requests
        self._per_seconds = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Return when a request may be sent now, waiting for the oldest one to leave the window."""
        async with self._lock:
            while True:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self._per_seconds:
                    self._stamps.popleft()
                if len(self._stamps) < self._max_requests:
                    self._stamps.append(now)
                    return
                await self._sleep(self._stamps[0] + self._per_seconds - now)


class RateLimitedTransport(httpx.AsyncBaseTransport):
    """Sends every request through ``inner`` after the limiter lets it go."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limiter: RateLimiter) -> None:
        self._inner = inner
        self._limiter = limiter

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._limiter.acquire()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class RetryAfterTooLong(httpx.HTTPError):
    """A host asked for a longer wait than ``MAX_RETRY_WAIT``; the request is not retried.

    ``status`` is the answer's HTTP status (429 or 503): the host refused the request rather than serving it.
    """

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a ``Retry-After`` value (whole seconds or an HTTP date), else None. A past date is 0."""
    value = (value or "").strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _retry_delay(response: httpx.Response, attempt: int) -> float | None:
    """Seconds to sleep before trying again, or None when this answer is final.

    Raises:
        RetryAfterTooLong: the host's Retry-After is above ``MAX_RETRY_WAIT``.
    """
    if response.status_code not in RETRY_STATUSES:
        return None
    raw = response.headers.get("retry-after")
    asked = parse_retry_after(raw)
    if asked is not None and asked > MAX_RETRY_WAIT:
        raise RetryAfterTooLong(
            f"{response.request.url.host} answered {response.status_code} with "
            f"Retry-After: {raw} (a wait of {asked:.0f}s; the limit is {MAX_RETRY_WAIT:g}s)",
            response.status_code,
        )
    if attempt >= MAX_ATTEMPTS:
        return None
    return asked if asked is not None else float(2 ** (attempt - 1))


_sleep = asyncio.sleep  # a name of its own so tests can replace it


class RetryingClient(httpx.AsyncClient):
    """An httpx client that waits and tries again when a host answers 429 or 503 (the same request again)."""

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        attempt = 1
        while True:
            response = await super().send(request, **kwargs)
            try:
                wait = _retry_delay(response, attempt)
            except RetryAfterTooLong:
                await response.aclose()
                raise
            if wait is None:
                return response
            await response.aclose()
            logger.warning(
                "%s answered %d; retrying in %gs (attempt %d of %d)",
                request.url.host,
                response.status_code,
                wait,
                attempt + 1,
                MAX_ATTEMPTS,
            )
            await _sleep(wait)
            attempt += 1


def build_http_client(
    *,
    user_agent: str,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
) -> RetryingClient:
    """An HTTP client with a polite User-Agent and no credentials."""
    return RetryingClient(
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        transport=transport,
    )


def limited_transport(per_minute: int, transport: httpx.AsyncBaseTransport | None = None) -> RateLimitedTransport:
    return RateLimitedTransport(transport or httpx.AsyncHTTPTransport(), RateLimiter(per_minute, 60.0))


def redact_text(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def describe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """What to say about a failed request: the type, the HTTP status and the URL, with ``secrets`` masked.

    An error that carries a request or response is described by its type, status and URL; its message is left out,
    because it is the URL again plus connection details. Any other error keeps its message, scrubbed.
    """
    secrets = tuple(secrets)
    parts = [type(exc).__name__]
    status = None
    url = None
    for attribute in ("response", "request"):
        try:
            source = getattr(exc, attribute, None)
        except Exception:  # noqa: BLE001 -- httpx raises RuntimeError for an unset request
            continue
        if status is None:
            status = getattr(source, "status_code", None)
        if url is None and getattr(source, "url", None):
            url = str(getattr(source, "url"))
    if isinstance(status, int):
        parts.append(str(status))
    if url:
        parts.append("for " + redact_text(url, secrets))
    elif str(exc):
        return f"{parts[0]}: {redact_text(str(exc), secrets)[:MAX_ERROR_TEXT]}"
    return " ".join(parts)
