"""ScrapeCreators (api.scrapecreators.com): Reddit, YouTube, X and Threads under the daily credit cap.

Each call costs its allowlist ``credits`` (1 for most endpoints), an empty answer included unless the entry's
``cost_note`` says otherwise; a cached answer (``cache_max_age``) and a transcript with no track cost none. A few
endpoints cost more, flat (``credits``, e.g. 26 for TikTok audience demographics) or when a paid option is on
(``paid_params``, up to ``credits_max``). Answers say what they cost (``credits_charged``, not every endpoint sends it)
and what is left (``credits_remaining``), and ``/v1/credit-balance`` is free.

The cap stays honest: before a call, ``reservation`` credits are reserved in the ledger (``credits``, or
``credits_max`` when a paid option is on); a call the cap does not allow is never sent (``CreditCapReached``). After
the answer the row is settled to its ``credits_charged`` when that is a non-negative number (a refund or an extra
charge); an answer without a valid one keeps the reservation. A failed call keeps its reservation, except a 4xx answer
(including a 429 that asked for too long a wait), which costs nothing. Paid options are refused unless the caller
passes ``allow_paid_params=True``; a paid option set to an off value (no, false, 0, ...) is dropped, never sent.

An option with no upper price (``allow_long_transcription``: 10 credits per started minute, no duration limit) is
open-ended spend. It is refused unless the global config sets ``allow_open_ended_spend: true`` (no argument can turn
it on); then the call reserves ``max_credits_per_open_ended_call`` and is refused when less is left today. That bounds
what scout lets through, not what ScrapeCreators bills: a long video can cost more than the reservation, and the
ledger records what the answer says it cost. Parameters and field names checked against the OpenAPI spec
(docs.scrapecreators.com/openapi.json) and real answers.

Parsers skip an element that is not the documented shape instead of failing the whole answer.

Only endpoints in ``scout/data/scrapecreators_endpoints.json`` can be called, by the typed methods and by the generic
``call`` alike; the allowlist says each endpoint's method, parameters, credits and paid options.

Quirks: Reddit search has no upvote ratio (subreddit listings do); ``trim=true`` drops a post's permalink and date, so
it is never sent; comments need the post's full URL; X search wants short queries; X ``user-tweets`` returns a big
account's most popular tweets, not its latest; Threads search answers empty often; some answers carry raw control
characters, so they are parsed with ``strict=False``.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from importlib import resources
from typing import Any, Self

import httpx

from scout.http import RetryAfterTooLong, build_http_client, describe_error, limited_transport, redact_text
from scout.models import Comment, Item, ProviderError, ScoutError
from scout.parsing import INT_LIMIT
from scout.parsing import as_int as _int
from scout.parsing import epoch as _epoch
from scout.parsing import iso as _iso
from scout.parsing import objects as _rows
from scout.parsing import parsed as _parsed
from scout.store import CreditLedger

BASE_URL = "https://api.scrapecreators.com"
# The OpenAPI document names the balance route /v1/account/credit-balance, and its example answer reports
# "credits_charged": 1. scout keeps the older /v1/credit-balance alias, which answers the same creditCount for free.
BALANCE_PATH = "/v1/credit-balance"
TEXT_LIMIT = 4000
# An open-ended call (a long AI transcription) can take minutes; the answer, with what it cost, is worth waiting for.
OPEN_ENDED_TIMEOUT = 300.0
# A credits_charged above this is not a price, it is a broken answer: the reservation is kept instead.
_MAX_CHARGE = 1_000_000
_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")


# The allowlist


@dataclass(frozen=True)
class EndpointParam:
    name: str
    required: bool
    description: str


_OFF_VALUES = ("", "0", "false", "no", "off", "none", "null")


def is_on(value: object) -> bool:
    """Whether a parameter value turns an option on (anything but empty, 0, false, no, off, none)."""
    return str(value).strip().lower() not in _OFF_VALUES


@dataclass(frozen=True)
class Endpoint:
    path: str
    method: str
    platform: str
    params: tuple[EndpointParam, ...]
    credits: int
    description: str
    group: str = ""
    credits_max: int | None = None
    paid_params: tuple[str, ...] = ()
    cost_note: str = ""

    @property
    def cacheable(self) -> bool:
        """Whether the endpoint takes ``cache_max_age`` (a cached answer is free)."""
        return any(p.name == "cache_max_age" for p in self.params)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "method": self.method,
            "platform": self.platform,
            "group": self.group,
            "params": [{"name": p.name, "required": p.required, "description": p.description} for p in self.params],
            "credits": self.credits,
            "credits_max": self.credits_max,
            "paid_params": list(self.paid_params),
            "cost_note": self.cost_note,
            "description": self.description,
        }

    def paid_options(self, params: dict[str, str]) -> list[str]:
        """The paid options ``params`` turns on."""
        return [name for name in self.paid_params if name in params and is_on(params[name])]

    def open_ended(self, params: dict[str, str]) -> bool:
        """Whether ``params`` turns on a paid option with no upper price (open-ended spend)."""
        return self.credits_max is None and bool(self.paid_options(params))

    def reservation(self, params: dict[str, str], open_ended_limit: int | None = None) -> int | None:
        """The credits a call with ``params`` reserves: ``credits``, or ``credits_max`` with a paid option on, or
        ``open_ended_limit`` for an option with no upper price (None when that is not allowed)."""
        if not self.paid_options(params):
            return self.credits
        return open_ended_limit if self.credits_max is None else self.credits_max

    def check(
        self, params: dict[str, str], *, allow_paid_params: bool = False, allow_open_ended: bool = False
    ) -> dict[str, str]:
        """``params`` as they are sent: every required one there, none unknown, a paid option set to an off value
        dropped and one set on sent as ``true``. ``ScoutError`` for a paid option without ``allow_paid_params``, or an
        open-ended one unless ``allow_open_ended`` (the global config's ``allow_open_ended_spend``)."""
        known = {p.name for p in self.params}
        unknown = sorted(set(params) - known)
        if unknown:
            raise ScoutError(
                f"{self.path} takes no parameter {', '.join(unknown)} (it takes {', '.join(sorted(known)) or 'none'})"
            )
        missing = [p.name for p in self.params if p.required and not str(params.get(p.name, "")).strip()]
        if missing:
            raise ScoutError(f"{self.path} needs {', '.join(missing)}")
        sent = {
            name: ("true" if is_on(value) else None) if name in self.paid_params else value
            for name, value in params.items()
        }
        params = {name: value for name, value in sent.items() if value is not None}
        paid = self.paid_options(params)
        if paid and self.open_ended(params) and not allow_open_ended:
            raise ScoutError(
                f"{', '.join(paid)} on {self.path} has no upper price at ScrapeCreators (10 credits per started minute "
                "of video); scout refuses open-ended spend unless allow_open_ended_spend: true is set in its "
                "config.yaml, and no argument can turn it on"
            )
        if paid and not allow_paid_params:
            raise ScoutError(
                f"{', '.join(paid)} on {self.path} costs extra ({self.cost_note.rstrip('.') or 'see list_endpoints'}); "
                "pass allow_paid_params (CLI: --allow-paid-params) only when the user asked for it"
            )
        return params


def _parse_endpoint(entry: dict[str, Any]) -> Endpoint:
    path = str(entry["path"])
    if not path.startswith("/v") or ".." in path or "?" in path:
        raise ValueError(f"allowlist path {path!r} is not an API path")
    method = str(entry.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        raise ValueError(f"allowlist method {method!r} of {path} is not GET or POST")
    credits = int(entry.get("credits", 1))
    if credits < 0:
        raise ValueError(f"allowlist credits of {path} is negative")
    params = tuple(
        EndpointParam(str(p["name"]), bool(p.get("required", False)), str(p.get("description") or ""))
        for p in entry.get("params") or []
    )
    raw_max = entry.get("credits_max")
    credits_max = int(raw_max) if raw_max is not None else None
    if credits_max is not None and credits_max < credits:
        raise ValueError(f"allowlist credits_max of {path} is below its credits")
    paid = tuple(str(name) for name in entry.get("paid_params") or [])
    unknown = sorted(set(paid) - {p.name for p in params})
    if unknown:
        raise ValueError(f"allowlist paid_params of {path} name unknown parameters: {', '.join(unknown)}")
    return Endpoint(
        path=path,
        method=method,
        platform=str(entry.get("platform") or ""),
        params=params,
        credits=credits,
        description=str(entry.get("description") or ""),
        group=str(entry.get("group") or ""),
        credits_max=credits_max,
        paid_params=paid,
        cost_note=str(entry.get("cost_note") or ""),
    )


@cache
def load_endpoints() -> dict[str, Endpoint]:
    """The allowlist, by path (``scout/data/scrapecreators_endpoints.json``: a list, or ``{"endpoints": [...]}``)."""
    text = resources.files("scout").joinpath("data/scrapecreators_endpoints.json").read_text(encoding="utf-8")
    data = json.loads(text)
    entries = data.get("endpoints") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("the endpoint allowlist is not a list")
    endpoints = [_parse_endpoint(e) for e in entries]
    return {e.path: e for e in endpoints}


def endpoint(path: str) -> Endpoint:
    """The allowlist entry of ``path`` (with or without the leading slash).

    Raises:
        ScoutError: the endpoint is not on the allowlist.
    """
    wanted = "/" + path.strip().lstrip("/")
    found = load_endpoints().get(wanted)
    if found is None:
        raise ScoutError(f"{wanted} is not on scout's ScrapeCreators allowlist (see `scout endpoints`)")
    return found


# Parsing helpers


def _ratio(value: Any) -> float | None:
    """A share between 0 and 1 (Reddit's upvote ratio), else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 <= number <= 1.0 else None


def credit_count(value: Any, limit: int = _MAX_CHARGE) -> int | None:
    """A credit count from an answer (``credits_charged``, ``credits_remaining``): a non-negative int, float or numeric
    string (a fraction rounds up) up to ``limit``, else None, so a negative, absurd or non-numeric value never settles
    the ledger."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = math.ceil(value)
    if not isinstance(value, int) or not 0 <= value <= limit:
        return None
    return value


def _object(body: Any, path: str) -> dict[str, Any]:
    """The answer of ``path`` when it is a JSON object, as every typed method reads it."""
    if isinstance(body, dict):
        return body
    raise ProviderError(f"{path} answered a JSON {type(body).__name__}, not an object")


def _headline(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:140]


def reddit_timeframe(days: int | None) -> str:
    if days is None or days > 365:
        return "all"
    return "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"


def youtube_upload_date(days: int | None) -> str | None:
    if days is None or days > 365:
        return None
    return "today" if days <= 1 else "this_week" if days <= 7 else "this_month" if days <= 31 else "this_year"


def channel_param(handle_or_id: str) -> dict[str, str]:
    """``{"channelId": ...}`` for a ``UC...`` channel id, else ``{"handle": ...}`` without the ``@``."""
    wanted = handle_or_id.strip()
    return {"channelId": wanted} if _CHANNEL_ID.match(wanted) else {"handle": wanted.lstrip("@")}


TRANSCRIPT_ENDPOINTS = {
    "youtube": "/v1/youtube/video/transcript",
    "tiktok": "/v1/tiktok/video/transcript",
    "instagram": "/v2/instagram/media/transcript",
    "x": "/v1/twitter/tweet/transcript",
    "reddit": "/v1/reddit/post/transcript",
}
COMMENT_ENDPOINTS = {
    "reddit": "/v1/reddit/post/comments",
    "youtube": "/v1/youtube/video/comments",
    "tiktok": "/v1/tiktok/video/comments",
    "instagram": "/v2/instagram/post/comments",
}
_VTT_TIME = re.compile(r"^(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->")
_VTT_TAG = re.compile(r"<[^>]+>")


def vtt_text(raw: str) -> str:
    """Plain text of a transcript that may be WebVTT (cue timings, numbers and tags dropped, repeats kept once)."""
    if not raw.lstrip().startswith("WEBVTT"):
        return raw.strip()
    lines: list[str] = []
    for line in raw.splitlines():
        line = _VTT_TAG.sub("", line).replace("\\n", " ").strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")) or line.isdigit():
            continue
        if _VTT_TIME.match(line):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return " ".join(lines)


def x_months(day: str, days: int | None) -> list[str]:
    """The months ("October 2026") that a window of ``days`` ending on ``day`` touches, newest first; none for no
    window or one longer than 31 days."""
    if days is None or days > 31:
        return []
    end = date.fromisoformat(day)
    month = date(end.year, end.month, 1)
    start = end - timedelta(days=days)
    months: list[str] = []
    while month >= date(start.year, start.month, 1):
        months.append(f"{month:%B %Y}")
        month = (month - timedelta(days=1)).replace(day=1)
    return months


def x_query(query: str, months: list[str]) -> str:
    """What X search sends: the query and the month (or months, joined with OR) its window touches."""
    if not months:
        return query
    if len(months) == 1:
        return f"{query} {months[0]}"
    return f"{query} " + " OR ".join(f'"{month}"' for month in months)


class ScrapeCreatorsClient:
    """One HTTP client for a run; ``async with`` closes it. ``credits_used`` counts what its calls cost.

    ``open_ended_limit`` is what one open-ended call may reserve (the global config's
    ``max_credits_per_open_ended_call`` when ``allow_open_ended_spend`` is on); None refuses every open-ended call.
    """

    def __init__(
        self,
        api_key: str,
        ledger: CreditLedger,
        daily_cap: int,
        *,
        day: str,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        open_ended_limit: int | None = None,
    ) -> None:
        self._ledger = ledger
        self._cap = daily_cap
        self._key = api_key
        self._day = day
        self._open_ended_limit = open_ended_limit
        self._client = build_http_client(
            user_agent=user_agent, timeout=timeout, transport=limited_transport(120, transport)
        )
        self._client.headers["x-api-key"] = api_key
        # httpx keeps custom headers on a cross-origin redirect; the key must never leave this host.
        self._client.follow_redirects = False
        self.calls = 0
        self.credits_used = 0
        self.remaining: int | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def day(self) -> str:
        return self._day

    async def balance(self) -> int:
        """The account's credits left (a free call, not counted)."""
        body = _object(await self._send("GET", BALANCE_PATH, {}), BALANCE_PATH)
        return _int(body.get("creditCount"))

    def _error(self, message: str, status: int | None = None) -> ProviderError:
        """A ``ProviderError`` with the key masked, whatever the answer echoed."""
        return ProviderError(redact_text(message, [self._key]), status=status)

    async def _send(self, method: str, path: str, params: dict[str, str], *, timeout: float | None = None) -> Any:
        """The answer of one request: a JSON object or list. ``ProviderError`` (with the HTTP status when there was an
        answer) for anything else."""
        extra: dict[str, Any] = {"timeout": timeout} if timeout else {}
        try:
            if method == "POST":
                response = await self._client.post(BASE_URL + path, json=params, **extra)
            else:
                response = await self._client.get(BASE_URL + path, params=params, **extra)
        except RetryAfterTooLong as exc:
            raise self._error(f"{method} {path} was refused: {exc}", exc.status) from exc
        except httpx.HTTPError as exc:
            raise self._error(f"{method} {path} failed: {describe_error(exc, [self._key])}") from exc
        try:
            body = json.loads(response.text, strict=False)
        except ValueError:
            body = None
        if response.status_code >= 400:
            message = body.get("message") if isinstance(body, dict) else None
            raise self._error(
                f"{method} {path} answered {response.status_code}" + (f": {str(message)[:200]}" if message else ""),
                response.status_code,
            )
        if not isinstance(body, dict | list):
            raise self._error(
                f"{method} {path} answered {response.status_code} with something that is not JSON",
                response.status_code,
            )
        return body

    async def call(self, path: str, params: dict[str, str], *, allow_paid_params: bool = False) -> Any:
        """A paid call to an allowlisted endpoint: reserve its credits, send, settle to what the answer says it cost.
        Returns the answer: a JSON object, or a list for the few endpoints that answer one.

        The reservation is ``Endpoint.reservation``. The row is settled to the answer's ``credits_charged`` when that is
        a valid count (refunding the difference or recording the extra) and keeps the reservation otherwise. A 4xx
        answer (the call was refused, not served) costs nothing; any other failure, a 200 that is not JSON included,
        keeps the reservation.

        Raises:
            ScoutError: the endpoint is not allowlisted, a required parameter is missing, a paid option is on without
                ``allow_paid_params``, or an open-ended one without ``open_ended_limit``.
            CreditCapReached: the cap does not allow the call; it was not sent.
            ProviderError: the call failed.
        """
        entry = endpoint(path)
        params = entry.check(
            {k: str(v) for k, v in params.items()},
            allow_paid_params=allow_paid_params,
            allow_open_ended=self._open_ended_limit is not None,
        )
        wanted = entry.reservation(params, self._open_ended_limit)
        if wanted is None:  # check() refuses this first; kept so no path can send an unbounded call
            raise ScoutError(f"{entry.path}: open-ended spend is off")
        if wanted == 0:
            return await self._send(entry.method, entry.path, params)
        row = self._ledger.reserve_credit(entry.path, self._cap, self._day, wanted)
        self.calls += 1
        timeout = OPEN_ENDED_TIMEOUT if entry.open_ended(params) else None
        try:
            body = await self._send(entry.method, entry.path, params, timeout=timeout)
        except ProviderError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                self._ledger.settle_credit(row, 0, None)
            else:
                self.credits_used += wanted
            raise
        fields = body if isinstance(body, dict) else {}
        charged = credit_count(fields.get("credits_charged"))
        remaining = credit_count(fields.get("credits_remaining"), INT_LIMIT)
        if remaining is not None:
            self.remaining = remaining
        cost = wanted if charged is None else charged
        self._ledger.settle_credit(row, cost, remaining)
        self.credits_used += cost
        return body

    # Reddit

    def _reddit_item(self, post: dict[str, Any], lens: str, watch: str) -> Item | None:
        if not post.get("id"):
            return None
        permalink = str(post.get("permalink") or "")
        link = str(post.get("url") or "")
        text = str(post.get("selftext") or "")[:TEXT_LIMIT]
        return Item(
            source="reddit",
            ext_id=str(post.get("id") or ""),
            url=f"https://www.reddit.com{permalink}" if permalink.startswith("/") else link,
            title=str(post.get("title") or ""),
            text=text,
            author=str(post.get("author") or ""),
            community=str(post.get("subreddit") or ""),
            created_at=_epoch(post.get("created_utc")),
            score=_int(post.get("score")),
            comments=_int(post.get("num_comments")),
            lens=lens,
            watch=watch,
            upvote_ratio=_ratio(post.get("upvote_ratio")),
            link=link if link and "reddit.com" not in link else None,
            extra={"domain": post.get("domain"), "flair": post.get("link_flair_text")},
        )

    async def reddit_subreddit(self, name: str, sort: str, timeframe: str, lens: str = "") -> list[Item]:
        name = name.strip().removeprefix("r/")
        path = "/v1/reddit/subreddit"
        body = _object(await self.call(path, {"subreddit": name, "sort": sort, "timeframe": timeframe}), path)
        watch = f"r/{name} {sort}/{timeframe}"
        return _parsed(_rows(body.get("posts")), lambda p: self._reddit_item(p, lens, watch), "Reddit post")

    async def reddit_search(self, query: str, timeframe: str, lens: str = "") -> list[Item]:
        path = "/v1/reddit/search"
        body = _object(await self.call(path, {"query": query, "sort": "relevance", "timeframe": timeframe}), path)
        watch = f"reddit: {query}"
        return _parsed(_rows(body.get("posts")), lambda p: self._reddit_item(p, lens, watch), "Reddit post")

    async def reddit_comments(self, item: Item) -> list[Comment]:
        return await self.reddit_comments_of(item.url, item.ext_id)

    async def reddit_comments_of(self, url: str, post_id: str) -> list[Comment]:
        path = "/v1/reddit/post/comments"
        body = _object(await self.call(path, {"url": url}), path)
        found: list[Comment] = []

        def one(node: dict[str, Any]) -> Comment | None:
            if not node.get("body") or node.get("author") in (None, "[deleted]", "AutoModerator"):
                return None
            return Comment(
                source="reddit",
                post_id=post_id,
                ext_id=str(node.get("id") or ""),
                author=str(node.get("author")),
                text=str(node["body"])[:1500],
                score=_int(node.get("score")),
                created_at=_epoch(node.get("created_utc")),
            )

        def walk(nodes: list[dict[str, Any]], depth: int) -> None:
            for node in nodes:
                found.extend(_parsed([node], one, "Reddit comment"))
                replies = node.get("replies")
                if depth < 1 and isinstance(replies, dict):
                    walk(_rows(replies.get("items")), depth + 1)

        walk(_rows(body.get("comments")), 0)
        return found

    # YouTube

    def _youtube_item(self, video: dict[str, Any], lens: str, watch: str) -> Item | None:
        if video.get("type", "video") != "video" or not video.get("id"):
            return None
        channel = video.get("channel") or {}
        views = _int(video.get("viewCountInt"))
        return Item(
            source="youtube",
            ext_id=str(video.get("id") or ""),
            url=str(video.get("url") or f"https://www.youtube.com/watch?v={video.get('id')}"),
            title=str(video.get("title") or ""),
            text=str(video.get("description") or "")[:TEXT_LIMIT],
            author=str(channel.get("title") or ""),
            community=str(channel.get("handle") or channel.get("id") or ""),
            created_at=_iso(video.get("publishedTime")),
            score=views,
            comments=_int(video.get("commentCountInt")),
            lens=lens,
            watch=watch,
            views=views,
            extra={
                "likes": _int(video.get("likeCountInt")),
                "seconds": _int(video.get("lengthSeconds")),
            },
        )

    async def youtube_channel(self, handle: str, sort: str, lens: str = "") -> list[Item]:
        """The channel's videos (30 per credit), each with its views over the channel's median: the outlier multiple
        (worked out once at least 5 videos have views).

        ``handle`` is a handle (``@name`` or ``name``) or a channel id (``UC...``).
        """
        wanted = handle.strip()
        params = channel_param(wanted)
        path = "/v1/youtube/channel-videos"
        body = _object(await self.call(path, {**params, "sort": sort}), path)
        label = wanted if "channelId" in params else f"@{wanted.lstrip('@')}"
        watch = f"youtube: {label}"
        items = _parsed(_rows(body.get("videos")), lambda v: self._youtube_item(v, lens, watch), "YouTube video")
        views = [i.score for i in items if i.score > 0]
        median = statistics.median(views) if len(views) >= 5 else None
        for item in items:
            item.extra["channel_median"] = median
            item.extra["outlier"] = round(item.score / median, 1) if median else None
        return items

    async def youtube_search(self, query: str, upload_date: str | None, lens: str = "") -> list[Item]:
        params = {"query": query, "type": "videos"}
        if upload_date:
            params["uploadDate"] = upload_date
        path = "/v1/youtube/search"
        body = _object(await self.call(path, params), path)
        watch = f"youtube: {query}"
        return _parsed(_rows(body.get("videos")), lambda v: self._youtube_item(v, lens, watch), "YouTube video")

    async def youtube_typeahead(self, seed: str) -> list[str]:
        """YouTube's autocomplete for ``seed`` (``*`` is a wildcard): what people type, a demand signal."""
        path = "/v1/youtube/search/typeahead"
        body = _object(await self.call(path, {"query": seed}), path)
        data = body.get("data")
        suggestions = body.get("suggestions") or (data.get("suggestions") if isinstance(data, dict) else None)
        lines: list[str] = []
        for suggestion in suggestions if isinstance(suggestions, list) else []:
            text = suggestion.get("text") if isinstance(suggestion, dict) else suggestion
            if isinstance(text, str) and text.strip():
                lines.append(text.strip()[:200])
        return lines

    async def youtube_comments(self, item: Item) -> list[Comment]:
        return await self.youtube_comments_of(item.url, item.ext_id)

    async def youtube_comments_of(self, url: str, video_id: str) -> list[Comment]:
        path = "/v1/youtube/video/comments"
        body = _object(await self.call(path, {"url": url, "order": "top"}), path)

        def one(c: dict[str, Any]) -> Comment | None:
            if not c.get("content"):
                return None
            return Comment(
                source="youtube",
                post_id=video_id,
                ext_id=str(c.get("id") or ""),
                author=str((c.get("author") or {}).get("name") or ""),
                text=str(c.get("content") or "")[:1500],
                score=_int((c.get("engagement") or {}).get("likes")),
                created_at=_iso(c.get("publishedTime")),
            )

        return _parsed(_rows(body.get("comments")), one, "YouTube comment")

    async def youtube_transcript(self, url: str, language: str | None = None) -> dict[str, Any]:
        params = {"url": url}
        if language:
            params["language"] = language
        path = "/v1/youtube/video/transcript"
        body = _object(await self.call(path, params), path)

        def segment(s: dict[str, Any]) -> dict[str, Any]:
            return {
                "text": str(s.get("text") or ""),
                "start_ms": _int(s.get("startMs")),
                "end_ms": _int(s.get("endMs")),
            }

        segments = _parsed(_rows(body.get("transcript")), segment, "transcript segment")
        text = str(body.get("transcript_only_text") or " ".join(s["text"] for s in segments))
        return {
            "video_id": body.get("videoId"),
            "url": body.get("url") or url,
            "language": body.get("language"),
            "text": text,
            "segments": segments,
        }

    # Comments and transcripts of other platforms, read by URL

    async def tiktok_comments_of(self, url: str, video_id: str) -> list[Comment]:
        """A TikTok video's comments (1 credit a page with comments; an empty page is free)."""
        path = "/v1/tiktok/video/comments"
        body = _object(await self.call(path, {"url": url}), path)

        def one(c: dict[str, Any]) -> Comment | None:
            if not c.get("text"):
                return None
            return Comment(
                source="tiktok",
                post_id=video_id,
                ext_id=str(c.get("cid") or ""),
                author=str((c.get("user") or {}).get("unique_id") or ""),
                text=str(c.get("text") or "")[:1500],
                score=_int(c.get("digg_count")),
                created_at=_epoch(c.get("create_time")),
            )

        return _parsed(_rows(body.get("comments")), one, "TikTok comment")

    async def instagram_comments_of(self, url: str, code: str) -> list[Comment]:
        """An Instagram post's or reel's comments, without replies (1 credit a page with comments; empty is free)."""
        path = "/v2/instagram/post/comments"
        body = _object(await self.call(path, {"url": url}), path)

        def one(c: dict[str, Any]) -> Comment | None:
            if not c.get("text"):
                return None
            return Comment(
                source="instagram",
                post_id=code,
                ext_id=str(c.get("id") or ""),
                author=str((c.get("user") or {}).get("username") or ""),
                text=str(c.get("text") or "")[:1500],
                score=_int(c.get("comment_like_count")),
                created_at=_iso(c.get("created_at")),
            )

        return _parsed(_rows(body.get("comments")), one, "Instagram comment")

    async def url_transcript(self, source: str, url: str, language: str | None = None) -> dict[str, Any]:
        """The transcript of a TikTok, Instagram, X or Reddit video by URL, at the flat price (1 credit; none when
        there is no transcript). Never turns on ``allow_long_transcription``: a video past 120 s that has no captions
        comes back without a transcript instead of costing 10 credits a minute."""
        path = TRANSCRIPT_ENDPOINTS[source]
        params = {"url": url}
        if language:
            params["language"] = language
        body = _object(await self.call(path, params), path)
        language_found = body.get("language")
        if source == "instagram":
            parts = [str(t.get("text") or "") for t in _rows(body.get("transcripts"))]
            text = "\n\n".join(p for p in parts if p)
        else:
            raw = body.get("transcript")
            text = vtt_text(raw) if isinstance(raw, str) else ""
        return {"url": body.get("url") or url, "language": language_found, "text": text, "segments": []}

    # X

    def _x_item(self, tweet: dict[str, Any], lens: str, watch: str) -> Item | None:
        legacy = tweet.get("legacy") or {}
        tweet_id = str(tweet.get("rest_id") or legacy.get("id_str") or "")
        if not tweet_id:
            return None
        note = ((tweet.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
        text = str(note.get("text") or legacy.get("full_text") or "")[:TEXT_LIMIT]
        user = ((tweet.get("core") or {}).get("user_results") or {}).get("result") or {}
        handle = str((user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name") or "")
        try:
            created = datetime.strptime(str(legacy.get("created_at")), "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            created = None
        views = _int((tweet.get("views") or {}).get("count"))
        return Item(
            source="x",
            ext_id=tweet_id,
            url=str(tweet.get("url") or f"https://x.com/{handle or 'i'}/status/{tweet_id}"),
            title=_headline(text),
            text=text,
            author=handle,
            community="x",
            created_at=created,
            score=_int(legacy.get("favorite_count")),
            comments=_int(legacy.get("reply_count")),
            lens=lens,
            watch=watch,
            views=views or None,
            extra={
                "reposts": _int(legacy.get("retweet_count")),
                "quotes": _int(legacy.get("quote_count")),
                "bookmarks": _int(legacy.get("bookmark_count")),
            },
        )

    async def x_search(self, query: str, lens: str = "", *, days: int | None = 7) -> list[Item]:
        """X posts for ``query`` from about the last ``days`` (None: any time).

        The endpoint searches Google's index, not X: without the month and year the posts are weeks to months old, so
        the month the window touches is added (each month, joined with OR, when the window crosses into an earlier
        one). Past 31 days nothing is added.
        """
        path = "/v1/twitter/search/posts"
        body = _object(await self.call(path, {"query": x_query(query, x_months(self._day, days))}), path)
        watch = f"x: {query}"
        return _parsed(_rows(body.get("posts")), lambda t: self._x_item(t, lens, watch), "X post")

    async def x_account(self, handle: str, lens: str = "") -> list[Item]:
        path = "/v1/twitter/user-tweets"
        body = _object(await self.call(path, {"handle": handle.lstrip("@")}), path)
        watch = f"x: @{handle.lstrip('@')}"
        return _parsed(_rows(body.get("tweets")), lambda t: self._x_item(t, lens, watch), "X post")

    # Threads

    def _threads_item(self, post: dict[str, Any], lens: str, watch: str) -> Item | None:
        code = str(post.get("code") or "")
        if not code:
            return None
        user = str((post.get("user") or {}).get("username") or "")
        text = str((post.get("caption") or {}).get("text") or "")[:TEXT_LIMIT]
        info = post.get("text_post_app_info") or {}
        return Item(
            source="threads",
            ext_id=code,
            url=str(post.get("url") or f"https://www.threads.com/@{user}/post/{code}"),
            title=_headline(text),
            text=text,
            author=user,
            community="threads",
            created_at=_epoch(post.get("taken_at")),
            score=_int(post.get("like_count")),
            comments=_int(info.get("direct_reply_count")),
            lens=lens,
            watch=watch,
            extra={
                "reposts": _int(info.get("repost_count")),
                "quotes": _int(info.get("quote_count")),
            },
        )

    async def threads_search(self, query: str, lens: str = "", *, days: int | None = 14) -> list[Item]:
        params = {"query": query}
        if days is not None:
            day = date.fromisoformat(self._day)
            params["start_date"] = (day - timedelta(days=days)).isoformat()
            params["end_date"] = day.isoformat()
        path = "/v1/threads/search"
        body = _object(await self.call(path, params), path)
        watch = f"threads: {query}"
        return _parsed(_rows(body.get("posts")), lambda p: self._threads_item(p, lens, watch), "Threads post")

    async def threads_user(self, handle: str, lens: str = "") -> list[Item]:
        handle = handle.lstrip("@")
        path = "/v1/threads/user/posts"
        body = _object(await self.call(path, {"handle": handle}), path)
        watch = f"threads: @{handle}"
        return _parsed(_rows(body.get("posts")), lambda p: self._threads_item(p, lens, watch), "Threads post")
