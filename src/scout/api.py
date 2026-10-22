"""scout's functions, shared by the CLI and the MCP server. Each returns JSON-serialisable data.

Every result carries ``credits_used`` (what the call cost in ScrapeCreators credits; 0 for the free sources) and, for
a paid call, ``credits_left_today`` (what the global daily cap still allows) and ``daily_credit_cap``. A paid call
reserves its credits in the ledger before it is sent; one the cap does not allow is never sent and comes back with
``skipped: true``. ``dry_run=True`` lists the calls and their credits and calls nothing. Open-ended spend (a paid option
with no upper price) is refused unless the global config turns it on (``allow_open_ended_spend``).

Only ``daily`` and ``collect`` store what they find (in the profile's store); the ad-hoc functions return their results
and keep nothing but the ledger rows of what they cost.

Paid (ScrapeCreators): Reddit, YouTube, X, Threads, and by URL the comments and transcripts of TikTok, Instagram and X
videos, plus any allowlisted endpoint through ``scrapecreators_call``. Free: Hacker News, GitHub, Bluesky, Wikipedia
page views. The key is ``SCRAPECREATORS_API_KEY`` from the environment, else the macOS Keychain; without it a paid call
raises ``MissingKeyError`` and the free ones still work.

Items come in one shape (``Item.to_dict``): source, id, key, url, title, text, author, community, created_at, score
(the source's main engagement count), comments, views, upvote_ratio, link and extra (the source's own fields).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

from scout.config import (
    LENS_NAMES,
    PAID_SOURCES,
    SOURCES,
    GlobalConfig,
    Profile,
    load_global_config,
    load_profile,
    local_now,
    tool_registry,
)
from scout.daily import Collection, brief_path, build_brief, describe_plan, free_plan, open_quietly, paid_plan
from scout.daily import collect as collect_items
from scout.keyless import KeylessSources
from scout.keys import KEY_NAME, get_api_key, key_source
from scout.models import CreditCapReached, Item, MissingKeyError, ProviderError, ScoutError
from scout.paths import config_dir, data_dir, profile_path, profiles_dir
from scout.scrapecreators import (
    COMMENT_ENDPOINTS,
    TRANSCRIPT_ENDPOINTS,
    Endpoint,
    ScrapeCreatorsClient,
    channel_param,
    is_on,
    load_endpoints,
    reddit_timeframe,
    x_months,
    x_query,
    youtube_upload_date,
)
from scout.scrapecreators import endpoint as allowlisted
from scout.store import CreditLedger, Store

SEARCH_SOURCES = SOURCES
USER_POST_SOURCES = ("reddit", "youtube", "x", "threads", "bluesky")
MAX_LIMIT = 100
# What ``scrapecreators_call`` sends as ``cache_max_age`` on an endpoint that takes it, unless the caller sets one:
# a repeat within a day is answered from ScrapeCreators' cache for 0 credits. ``cache_max_age=none`` asks for a live call.
DEFAULT_CACHE_MAX_AGE = "1d"
_REDDIT_POST = re.compile(r"^/(?:r/[\w-]+/|u/[\w-]+/|user/[\w-]+/)?comments/([A-Za-z0-9]+)(?:/|$)")
_REDDIT_SHORT = re.compile(r"^/([A-Za-z0-9]+)/?$")
_YOUTUBE_ID = re.compile(r"^[\w-]{11}$")
_TIKTOK_VIDEO = re.compile(r"/(?:video|photo)/(\d+)")
_INSTAGRAM_POST = re.compile(r"^/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)")
_X_STATUS = re.compile(r"/status(?:es)?/(\d+)")

T = TypeVar("T")


# Plumbing


@dataclass
class _Context:
    settings: GlobalConfig
    day: str
    now: datetime


def _context() -> _Context:
    settings = load_global_config()
    now = local_now(settings)
    return _Context(settings=settings, day=now.date().isoformat(), now=now)


def _limit(value: int) -> int:
    if value < 1:
        raise ScoutError("limit must be at least 1")
    return min(value, MAX_LIMIT)


def _no_key() -> MissingKeyError:
    return MissingKeyError(
        f"no ScrapeCreators key found: set {KEY_NAME} or add it to the macOS Keychain; only the free sources run"
    )


def _left(ctx: _Context) -> int:
    ledger = CreditLedger.default()
    try:
        return ledger.left(ctx.day, ctx.settings.daily_credit_cap)
    finally:
        ledger.close()


def _plan(calls: list[dict[str, Any]], ctx: _Context, *, paid: bool) -> dict[str, Any]:
    needed = sum(int(c.get("credits", 0)) for c in calls)
    out: dict[str, Any] = {"dry_run": True, "calls": calls, "credits_needed": needed, "credits_used": 0}
    if paid:
        left = _left(ctx)
        out.update(
            credits_left_today=left,
            daily_credit_cap=ctx.settings.daily_credit_cap,
            fits_today=needed <= left,
            key_found=key_source() is not None,
        )
    return out


async def _paid(
    ctx: _Context, action: Callable[[ScrapeCreatorsClient], Awaitable[T]]
) -> tuple[T | None, dict[str, Any]]:
    """Run ``action`` with a ScrapeCreators client; (its result or None when the cap refused, the credit fields)."""
    key = get_api_key()
    if not key:
        raise _no_key()
    ledger = CreditLedger.default()
    cap = ctx.settings.daily_credit_cap
    try:
        async with _client(key, ledger, ctx) as client:
            try:
                result: T | None = await action(client)
                fields: dict[str, Any] = {}
            except CreditCapReached as exc:
                result = None
                fields = {"skipped": True, "reason": f"Credit cap: {exc}; the call was not sent"}
            fields.update(
                credits_used=client.credits_used,
                credits_left_today=ledger.left(ctx.day, cap),
                daily_credit_cap=cap,
            )
            return result, fields
    finally:
        ledger.close()


def _client(key: str, ledger: CreditLedger, ctx: _Context) -> ScrapeCreatorsClient:
    """A ScrapeCreators client under the global cap, with open-ended spend only as the global config allows it."""
    return ScrapeCreatorsClient(
        key,
        ledger,
        ctx.settings.daily_credit_cap,
        day=ctx.day,
        user_agent=ctx.settings.user_agent,
        open_ended_limit=ctx.settings.open_ended_limit,
    )


async def _free(ctx: _Context, action: Callable[[KeylessSources], Awaitable[T]]) -> T:
    async with KeylessSources(ctx.settings.user_agent) as free:
        return await action(free)


def _source(source: str, allowed: Sequence[str]) -> str:
    wanted = source.strip().lower()
    if wanted == "twitter":
        wanted = "x"
    if wanted not in allowed:
        raise ScoutError(f"unknown source {source!r}: use one of {', '.join(allowed)}")
    return wanted


def _sources(sources: Sequence[str] | None) -> set[str]:
    """The sources a profile run reads: the named ones (any case, ``twitter`` for x), else all of them."""
    return {_source(s, SOURCES) for s in sources} if sources else set(SOURCES)


def _within(items: list[Item], since: datetime | None) -> tuple[list[Item], int]:
    """``items`` from ``since`` on, and how many older ones were dropped; an item with no date is kept and marked
    ``extra.undated``."""
    if since is None:
        return items, 0
    kept: list[Item] = []
    for item in items:
        if item.created_at is None:
            item.extra["undated"] = True
            kept.append(item)
        elif item.created_at >= since:
            kept.append(item)
    return kept, len(items) - len(kept)


# Search


async def search(
    source: str,
    query: str,
    limit: int = 20,
    since_days: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Search one source for ``query``: posts, videos or repositories from the last ``since_days`` (None: any time).

    Paid: reddit, youtube, x, threads (1 credit each). Free: hn, github, bluesky. X searches Google's index of X, so
    for ``since_days`` of 31 or less the month and year are added to the query (each month the window touches), as
    recent posts need them. Reddit and YouTube ask for their own windows (day, week, month, year); every paid answer is
    then cut to ``since_days`` by date (``older_dropped`` says how many went; an item with no date is kept and marked
    ``extra.undated``).
    """
    source = _source(source, SEARCH_SOURCES)
    query = query.strip()
    if not query:
        raise ScoutError("query must not be empty")
    limit = _limit(limit)
    if since_days is not None and since_days < 1:
        raise ScoutError("since_days must be at least 1 (or None for any time)")
    ctx = _context()
    since = ctx.now - timedelta(days=since_days) if since_days is not None else None
    head: dict[str, Any] = {"source": source, "query": query, "since_days": since_days}

    if source in PAID_SOURCES:
        if source == "reddit":
            path, params = (
                "/v1/reddit/search",
                {"query": query, "sort": "relevance", "timeframe": reddit_timeframe(since_days)},
            )
        elif source == "youtube":
            upload = youtube_upload_date(since_days)
            path, params = (
                "/v1/youtube/search",
                {"query": query, "type": "videos", **({"uploadDate": upload} if upload else {})},
            )
        elif source == "x":
            path, params = "/v1/twitter/search/posts", {"query": x_query(query, x_months(ctx.day, since_days))}
        else:
            path, params = "/v1/threads/search", {"query": query}
        if dry_run:
            return {
                **head,
                **_plan([{"endpoint": path, "params": params, "credits": allowlisted(path).credits}], ctx, paid=True),
            }

        async def run(sc: ScrapeCreatorsClient) -> tuple[list[dict[str, Any]], int]:
            if source == "reddit":
                found = await sc.reddit_search(query, reddit_timeframe(since_days))
            elif source == "youtube":
                found = await sc.youtube_search(query, youtube_upload_date(since_days))
            elif source == "x":
                found = await sc.x_search(query, days=since_days)
            else:
                found = await sc.threads_search(query, days=since_days)
            kept, dropped = _within(found, since)
            return [i.to_dict() for i in kept[:limit]], dropped

        outcome, fields = await _paid(ctx, run)
        items, dropped = outcome or ([], 0)
        extra = {"older_dropped": dropped} if since is not None else {}
        return {**head, "count": len(items), "items": items, **extra, **fields}

    if dry_run:
        return {**head, **_plan([{"source": source, "query": query, "credits": 0}], ctx, paid=False)}

    async def run_free(free: KeylessSources) -> list[dict[str, Any]]:
        if source == "hn":
            found = await free.hn_search(query, -1, since, limit=limit, ad_hoc=True)
        elif source == "github":
            found = await free.github_search(query, None, since.date() if since else None, limit=limit)
        else:
            found = await free.bluesky_search(query, since, limit=limit)
        return [i.to_dict() for i in found[:limit]]

    items = await _free(ctx, run_free)
    return {**head, "count": len(items), "items": items, "credits_used": 0}


async def user_posts(source: str, handle_or_id: str, limit: int = 20, dry_run: bool = False) -> dict[str, Any]:
    """An account's latest posts: a subreddit's new posts, a YouTube channel's videos (handle or UC... id), an X
    account's tweets (a big account's most popular, not its latest), a Threads user's posts, or a Bluesky author feed.

    Paid: reddit, youtube, x, threads (1 credit each). Free: bluesky.
    """
    source = _source(source, USER_POST_SOURCES)
    handle = handle_or_id.strip()
    if not handle:
        raise ScoutError("handle_or_id must not be empty")
    limit = _limit(limit)
    ctx = _context()
    head: dict[str, Any] = {"source": source, "handle": handle}

    if source == "bluesky":
        if dry_run:
            return {**head, **_plan([{"source": "bluesky", "handle": handle, "credits": 0}], ctx, paid=False)}
        found = await _free(ctx, lambda free: free.bluesky_author_feed(handle, limit=limit))
        items = [i.to_dict() for i in found[:limit]]
        return {**head, "count": len(items), "items": items, "credits_used": 0}

    paths = {
        "reddit": ("/v1/reddit/subreddit", {"subreddit": handle.removeprefix("r/"), "sort": "new", "timeframe": "day"}),
        "youtube": ("/v1/youtube/channel-videos", {**channel_param(handle), "sort": "latest"}),
        "x": ("/v1/twitter/user-tweets", {"handle": handle.lstrip("@")}),
        "threads": ("/v1/threads/user/posts", {"handle": handle.lstrip("@")}),
    }
    path, params = paths[source]
    if dry_run:
        return {
            **head,
            **_plan([{"endpoint": path, "params": params, "credits": allowlisted(path).credits}], ctx, paid=True),
        }

    async def run(sc: ScrapeCreatorsClient) -> list[dict[str, Any]]:
        if source == "reddit":
            found = await sc.reddit_subreddit(handle, "new", "day")
        elif source == "youtube":
            found = await sc.youtube_channel(handle, "latest")
        elif source == "x":
            found = await sc.x_account(handle)
        else:
            found = await sc.threads_user(handle)
        return [i.to_dict() for i in found[:limit]]

    items, fields = await _paid(ctx, run)
    items = items or []
    return {**head, "count": len(items), "items": items, **fields}


# Comments and transcripts


def _url_source(url: str) -> tuple[str, str]:
    """(source, post or video id) of a YouTube, TikTok, Instagram, X, Reddit or Hacker News URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise ScoutError(f"{url!r} is not an http(s) URL")
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("old.")
    if host in ("reddit.com", "redd.it") or host.endswith(".reddit.com"):
        found = (_REDDIT_SHORT if host == "redd.it" else _REDDIT_POST).match(parts.path)
        if not found:
            raise ScoutError(
                f"{url} is not a Reddit post URL (use the post's full link: https://www.reddit.com/r/SUB/comments/ID/...)"
            )
        return "reddit", found.group(1)
    if host in ("youtube.com", "youtu.be", "music.youtube.com"):
        if host == "youtu.be":
            video = parts.path.strip("/").split("/")[0]
        elif parts.path.startswith(("/shorts/", "/live/", "/embed/")):
            video = parts.path.split("/")[2]
        else:
            video = (parse_qs(parts.query).get("v") or [""])[0]
        if not _YOUTUBE_ID.match(video):
            raise ScoutError(f"{url} is not a YouTube video URL")
        return "youtube", video
    if host == "news.ycombinator.com":
        story = (parse_qs(parts.query).get("id") or [""])[0]
        if not story.isdigit():
            raise ScoutError(f"{url} is not a Hacker News item URL")
        return "hn", story
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        found = _TIKTOK_VIDEO.search(parts.path)
        if found:
            return "tiktok", found.group(1)
        if host in ("vm.tiktok.com", "vt.tiktok.com") and parts.path.strip("/"):
            return "tiktok", parts.path.strip("/").split("/")[0]
        raise ScoutError(f"{url} is not a TikTok video URL")
    if host in ("instagram.com", "instagr.am"):
        found = _INSTAGRAM_POST.search(parts.path)
        if not found:
            raise ScoutError(f"{url} is not an Instagram post or reel URL")
        return "instagram", found.group(1)
    if host in ("x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"):
        found = _X_STATUS.search(parts.path)
        if not found:
            raise ScoutError(f"{url} is not an X post URL")
        return "x", found.group(1)
    raise ScoutError(
        f"{host or url!r} is not a supported host: comments and transcripts are read from YouTube, TikTok, Instagram, "
        "X (transcripts only), Reddit and Hacker News (comments only, free) URLs"
    )


def _comment_endpoint(source: str) -> str | None:
    """The paid endpoint that reads ``source``'s comments, None for Hacker News (free)."""
    if source == "hn":
        return None
    if source == "x":
        raise ScoutError("ScrapeCreators has no endpoint for the replies to an X post; search X for the topic instead")
    return COMMENT_ENDPOINTS[source]


def _transcript_endpoint(source: str) -> str:
    if source == "hn":
        raise ScoutError("a Hacker News story has no transcript; use comments for its discussion")
    return TRANSCRIPT_ENDPOINTS[source]


async def comments(url: str, limit: int = 50, dry_run: bool = False) -> dict[str, Any]:
    """The comments of a Reddit post, a YouTube, TikTok or Instagram video or post (1 credit a page; TikTok and
    Instagram pages with no comments cost nothing; top comments first where the source sorts), or of a Hacker News
    story (free). X has no comments endpoint and is refused."""
    source, post_id = _url_source(url)
    path = _comment_endpoint(source)
    limit = _limit(limit)
    ctx = _context()
    head: dict[str, Any] = {"url": url, "source": source, "id": post_id}
    if path is None:
        if dry_run:
            return {**head, **_plan([{"source": "hn", "url": url, "credits": 0}], ctx, paid=False)}
        found = await _free(ctx, lambda free: free.hn_comments_of(post_id, limit))
        rows = [c.to_dict() for c in found[:limit]]
        return {**head, "count": len(rows), "comments": rows, "credits_used": 0}

    if dry_run:
        return {
            **head,
            **_plan([{"endpoint": path, "params": {"url": url}, "credits": allowlisted(path).credits}], ctx, paid=True),
        }

    async def run(sc: ScrapeCreatorsClient) -> list[dict[str, Any]]:
        if source == "reddit":
            found = await sc.reddit_comments_of(url, post_id)
        elif source == "youtube":
            found = await sc.youtube_comments_of(url, post_id)
        elif source == "tiktok":
            found = await sc.tiktok_comments_of(url, post_id)
        else:
            found = await sc.instagram_comments_of(url, post_id)
        return [c.to_dict() for c in found[:limit]]

    rows, fields = await _paid(ctx, run)
    rows = rows or []
    return {**head, "count": len(rows), "comments": rows, **fields}


async def transcript(url: str, dry_run: bool = False, language: str | None = None) -> dict[str, Any]:
    """The transcript of a YouTube, TikTok, Instagram, X or Reddit video: its full text (and timed segments for
    YouTube). 1 credit; nothing when there is no transcript. Always the flat price: long AI transcription is never
    turned on here (only ``scrapecreators_call`` can, and only when the global config allows open-ended spend).
    ``language`` is taken by YouTube and TikTok only."""
    source, video_id = _url_source(url)
    path = _transcript_endpoint(source)
    ctx = _context()
    params = {"url": url, **({"language": language} if language else {})}
    allowlisted(path).check(params)
    head: dict[str, Any] = {"url": url, "source": source, "id": video_id}
    if dry_run:
        return {
            **head,
            **_plan([{"endpoint": path, "params": params, "credits": allowlisted(path).credits}], ctx, paid=True),
        }
    if source == "youtube":
        found, fields = await _paid(ctx, lambda sc: sc.youtube_transcript(url, language))
    else:
        found, fields = await _paid(ctx, lambda sc: sc.url_transcript(source, url, language))
    return {**head, **(found or {}), **fields}


def _param_text(value: Any) -> str:
    """A parameter as ScrapeCreators expects it: booleans as ``true``/``false``, everything else as text."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _with_cache(entry: Endpoint, params: dict[str, str]) -> dict[str, str]:
    """``params`` with ``cache_max_age`` defaulted on an endpoint that takes it, or removed when set to none/0/off."""
    if not entry.cacheable:
        return params
    if "cache_max_age" not in params:
        return {**params, "cache_max_age": DEFAULT_CACHE_MAX_AGE}
    if not is_on(params["cache_max_age"]):
        return {k: v for k, v in params.items() if k != "cache_max_age"}
    return params


def _cost(entry: Endpoint, params: dict[str, str], settings: GlobalConfig) -> dict[str, Any]:
    """What a call to ``entry`` with ``params`` reserves, for a plan or an estimate."""
    limit = settings.max_credits_per_open_ended_call
    wanted = entry.reservation(params, limit)
    out: dict[str, Any] = {
        "credits": wanted,
        "credits_max": entry.credits_max,
        "paid_options": entry.paid_options(params),
    }
    if entry.open_ended(params):
        out["open_ended"] = True
        if settings.allow_open_ended_spend:
            out["note"] = (
                f"no upper price at ScrapeCreators: scout reserves max_credits_per_open_ended_call ({limit}) and "
                "refuses the call when less is left today; ScrapeCreators may bill more than that for a long video"
            )
        else:
            out["refused"] = True
            out["note"] = "open-ended spend is off: set allow_open_ended_spend: true in config.yaml to allow it"
    if entry.cost_note:
        out["cost_note"] = entry.cost_note
    return out


async def scrapecreators_call(
    endpoint: str, params: dict[str, Any], dry_run: bool = False, allow_paid_params: bool = False
) -> dict[str, Any]:
    """Any allowlisted ScrapeCreators endpoint (``scout endpoints``), with its parameters; the raw answer.

    The allowlist says the endpoint's credits, which are reserved before the call (``credits_max`` when a paid option
    is on) and settled to what the answer says it cost; an endpoint not on it is refused, and so is a missing required
    parameter, an unknown one, or a paid option (``paid_params``) without ``allow_paid_params``. A paid option with no
    upper price is refused unless the global config sets ``allow_open_ended_spend`` (no argument turns it on). On an
    endpoint that takes ``cache_max_age`` it is sent as ``DEFAULT_CACHE_MAX_AGE`` unless given (``none`` for a live
    call).
    """
    entry = allowlisted(endpoint)
    given = {str(k): _param_text(v) for k, v in (params or {}).items()}
    ctx = _context()
    checked = entry.check(
        given, allow_paid_params=allow_paid_params, allow_open_ended=ctx.settings.allow_open_ended_spend
    )
    sent = _with_cache(entry, checked)
    head: dict[str, Any] = {"endpoint": entry.path, "params": sent}
    if dry_run:
        cost = _cost(entry, sent, ctx.settings)
        return {**head, **_plan([{"endpoint": entry.path, "params": sent, **cost}], ctx, paid=True)}
    body, fields = await _paid(ctx, lambda sc: sc.call(entry.path, sent, allow_paid_params=allow_paid_params))
    return {**head, "response": body, **fields}


async def wikipedia_pageviews(article: str, days: int = 30) -> dict[str, Any]:
    """Daily page views of an English Wikipedia article over the last ``days`` full days (free)."""
    if not article.strip():
        raise ScoutError("article must not be empty")
    if not 1 <= days <= 3650:
        raise ScoutError("days must be between 1 and 3650")
    ctx = _context()
    end = ctx.now.date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    found = await _free(ctx, lambda free: free.wikipedia_pageviews(article, start, end))
    rows = found.pop("days")
    return {
        **found,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(rows),
        "items": rows,
        "credits_used": 0,
    }


# Credits and estimates


async def credits() -> dict[str, Any]:
    """The ScrapeCreators balance (a free call), what scout used today by endpoint, and what the cap still allows."""
    ctx = _context()
    cap = ctx.settings.daily_credit_cap
    key = get_api_key()
    ledger = CreditLedger.default()
    try:
        balance = None
        external = None
        if key:
            async with _client(key, ledger, ctx) as client:
                balance = await client.balance()
            external = ledger.record_balance(balance, ctx.day)
        return {
            "key_found": key is not None,
            "balance": balance,
            "used_elsewhere_since_last_check": external,
            "used_elsewhere_today": ledger.external(ctx.day),
            "used_today": ledger.spent(ctx.day),
            "daily_credit_cap": cap,
            "credits_left_today": ledger.left(ctx.day, cap),
            "by_endpoint": [
                {"endpoint": e, "calls": calls, "credits": spent} for e, calls, spent in ledger.endpoint_use(ctx.day)
            ],
            "day": ctx.day,
            "credits_used": 0,
        }
    finally:
        ledger.close()


def estimate(
    *,
    profile: str | None = None,
    function: str | None = None,
    source: str | None = None,
    url: str | None = None,
    endpoint: str | None = None,
    sources: Sequence[str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What a call or a profile's daily run would cost in credits; calls nothing.

    Give ``profile`` (a daily/collect run, optionally limited to ``sources``), or ``endpoint`` (an allowlisted path,
    with the ``params`` you would send, so a paid option counts at its ``credits_max``), or ``function`` (search,
    user_posts, comments, transcript, pageviews) with its ``source`` or ``url``.
    """
    ctx = _context()
    out: dict[str, Any]
    if profile is not None:
        loaded = load_profile(profile, cap=ctx.settings.daily_credit_cap)
        wanted = _sources(sources)
        paid = paid_plan(loaded, wanted, {})
        _, data = describe_plan(loaded, paid, free_plan(loaded, wanted, ctx.now), ctx.settings.daily_credit_cap)
        out = {"profile": profile, **data, "credits": data["credits_max"]}
    elif endpoint is not None:
        entry = allowlisted(endpoint)
        wanted = {str(k): _param_text(v) for k, v in (params or {}).items()}
        out = {"endpoint": entry.path, **_cost(entry, wanted, ctx.settings)}
    elif function is not None:
        name = function.strip().lower().replace("-", "_")
        if name in ("search", "user_posts"):
            if not source:
                raise ScoutError(f"{name} needs a source")
            src = _source(source, SEARCH_SOURCES if name == "search" else USER_POST_SOURCES)
            cost = 1 if src in PAID_SOURCES else 0
        elif name in ("comments", "transcript"):
            if not url:
                raise ScoutError(f"{name} needs a url")
            src, _ = _url_source(url)
            path = _comment_endpoint(src) if name == "comments" else _transcript_endpoint(src)
            cost = allowlisted(path).credits if path else 0
        elif name in ("pageviews", "wikipedia_pageviews"):
            src, cost = "wikipedia", 0
        else:
            raise ScoutError("function must be search, user_posts, comments, transcript or pageviews")
        out = {"function": name, "source": src, "credits": cost}
    else:
        raise ScoutError("give a profile, an endpoint or a function to estimate")
    return {
        **out,
        "credits_used": 0,
        "credits_left_today": _left(ctx),
        "daily_credit_cap": ctx.settings.daily_credit_cap,
        "key_found": key_source() is not None,
    }


# Profiles: daily, collect, brief


def _load(profile: str, config_path: str | None, ctx: _Context) -> Profile:
    path = Path(config_path).expanduser() if config_path else None
    return load_profile(profile, path=path, cap=ctx.settings.daily_credit_cap)


async def _credits_line(ledger: CreditLedger, sc: ScrapeCreatorsClient | None, ctx: _Context) -> dict[str, Any]:
    balance = None
    if sc is not None:
        try:
            balance = await sc.balance()
            ledger.record_balance(balance, ctx.day)
        except ProviderError:
            balance = ledger.last_balance()
    return {
        "spent": ledger.spent(ctx.day),
        "cap": ctx.settings.daily_credit_cap,
        "balance": balance,
        "external": ledger.external(ctx.day),
    }


async def _run(
    profile: str,
    *,
    config_path: str | None,
    sources: Sequence[str] | None,
    dry_run: bool,
    write_brief: bool,
    use_claude: bool = True,
    force: bool = False,
    open_brief: bool = False,
) -> dict[str, Any]:
    ctx = _context()
    loaded = _load(profile, config_path, ctx)
    wanted = _sources(sources)
    cap = ctx.settings.daily_credit_cap
    path = brief_path(loaded.name, ctx.day)
    head: dict[str, Any] = {"profile": loaded.name, "day": ctx.day}
    if write_brief and path.exists() and not force and not dry_run:
        return {
            **head,
            "status": "exists",
            "brief_path": str(path),
            "credits_used": 0,
            "credits_left_today": _left(ctx),
        }
    key = None if dry_run else get_api_key()
    ledger = CreditLedger.default()
    store = Store.for_profile(loaded.name)
    try:
        async with KeylessSources(ctx.settings.user_agent) as free:
            sc = _client(key, ledger, ctx) if key else None
            try:
                if dry_run:
                    result: Collection = await collect_items(
                        loaded, store, None, free, sources=wanted, now=ctx.now, cap=cap, dry_run=True
                    )
                    found = key_source() is not None
                    lines = list(result.plan)
                    if not found and wanted & set(PAID_SOURCES):
                        lines.append(f"{KEY_NAME} is not set: the paid calls would be skipped")
                    return {
                        **head,
                        "dry_run": True,
                        "plan": lines,
                        **result.plan_data,
                        "key_found": found,
                        "credits_used": 0,
                        "credits_left_today": ledger.left(ctx.day, cap),
                    }
                warnings = [] if key else [f"{KEY_NAME} is not set: only the free sources ran"]
                if sc is not None:
                    await _credits_line(ledger, sc, ctx)
                result = await collect_items(loaded, store, sc, free, sources=wanted, now=ctx.now, cap=cap)
                warnings += result.warnings
                credit_line = await _credits_line(ledger, sc, ctx)
                out: dict[str, Any] = {
                    **head,
                    "status": "collected",
                    "items": len(result.items),
                    "paid_calls": result.paid_calls,
                    "skipped": result.skipped,
                    "warnings": warnings,
                    "credits": credit_line,
                    "credits_used": result.credits_used,
                    "credits_left_today": ledger.left(ctx.day, cap),
                    "daily_credit_cap": cap,
                }
                if not write_brief:
                    return out
                written = await build_brief(
                    loaded,
                    store,
                    tool_registry(loaded),
                    free,
                    day=ctx.day,
                    now=ctx.now,
                    demand=result.demand,
                    warnings=warnings,
                    use_claude=use_claude,
                    credits=credit_line,
                )
            finally:
                if sc is not None:
                    await sc.aclose()
    finally:
        store.close()
        ledger.close()
    if open_brief:
        open_quietly(written)
    return {**out, "status": "done", "brief_path": str(written)}


async def daily(
    profile: str = "default",
    *,
    sources: Sequence[str] | None = None,
    dry_run: bool = False,
    use_claude: bool = True,
    force: bool = False,
    open_brief: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """A profile's daily run: collect within the cap, then write today's brief (once a day unless ``force``)."""
    return await _run(
        profile,
        config_path=config_path,
        sources=sources,
        dry_run=dry_run,
        write_brief=True,
        use_claude=use_claude,
        force=force,
        open_brief=open_brief,
    )


async def collect(
    profile: str = "default",
    *,
    sources: Sequence[str] | None = None,
    dry_run: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Collect and store a profile's items within the cap; no brief."""
    return await _run(profile, config_path=config_path, sources=sources, dry_run=dry_run, write_brief=False)


async def brief(
    date: str | None = None,
    profile: str = "default",
    *,
    use_claude: bool = True,
    open_brief: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Build a day's brief again from the profile's store (default today). Makes no paid call."""
    ctx = _context()
    loaded = _load(profile, config_path, ctx)
    day = Date.fromisoformat(date).isoformat() if date else ctx.day
    store = Store.for_profile(loaded.name)
    ledger = CreditLedger.default()
    try:
        notes = store.run_notes(day) or {}
        now = datetime.fromisoformat(notes["now"]) if notes.get("now") else ctx.now
        credit_line = {
            "spent": ledger.spent(day),
            "cap": ctx.settings.daily_credit_cap,
            "balance": ledger.last_balance(),
            "external": ledger.external(day),
        }
        async with KeylessSources(ctx.settings.user_agent) as free:
            path = await build_brief(
                loaded,
                store,
                tool_registry(loaded),
                free,
                day=day,
                now=now,
                demand=notes.get("demand") or {},
                warnings=list(notes.get("warnings") or []),
                use_claude=use_claude,
                credits=credit_line,
            )
    finally:
        store.close()
        ledger.close()
    if open_brief:
        open_quietly(path)
    return {"profile": loaded.name, "day": day, "status": "done", "brief_path": str(path), "credits_used": 0}


# Listings


def endpoints(platform: str | None = None, query: str | None = None) -> dict[str, Any]:
    """The ScrapeCreators endpoints scout may call (the allowlist), with their parameters and credits.

    ``platform`` (e.g. tiktok; ``all`` for every one) and ``query`` (words that must all appear in the path, group or
    description) filter the list; without either, the full list is returned.
    """
    rows = list(load_endpoints().values())
    if platform and platform.strip().lower() != "all":
        wanted = platform.strip().lower()
        wanted = "twitter" if wanted == "x" else wanted
        rows = [e for e in rows if e.platform.lower() == wanted]
    if query and query.strip():
        words = query.lower().split()
        rows = [e for e in rows if all(w in f"{e.path} {e.group} {e.description}".lower() for w in words)]
    out = [e.to_dict() for e in rows]
    return {"count": len(out), "endpoints": out, "credits_used": 0}


def endpoint_platforms() -> dict[str, int]:
    """How many allowlisted endpoints each platform has."""
    counts: dict[str, int] = {}
    for entry in load_endpoints().values():
        counts[entry.platform] = counts.get(entry.platform, 0) + 1
    return dict(sorted(counts.items()))


def profiles() -> dict[str, Any]:
    """The profiles in the config folder, each with its lenses, or the error that stops it from loading."""
    cap = load_global_config().daily_credit_cap
    rows: list[dict[str, Any]] = []
    folder = profiles_dir()
    for file in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        row: dict[str, Any] = {"name": file.stem, "path": str(file)}
        try:
            loaded = load_profile(file.stem, cap=cap)
            row.update(
                valid=True,
                lenses=[name for name in LENS_NAMES if name in loaded.lenses],
                credit_budget=sum(lens.credit_budget for lens in loaded.lenses.values()),
            )
        except (OSError, ValueError) as exc:
            row.update(valid=False, error=str(exc).splitlines()[0][:300])
        rows.append(row)
    return {
        "config_dir": str(config_dir()),
        "data_dir": str(data_dir()),
        "profiles": rows,
        "default_exists": profile_path("default").is_file(),
        "credits_used": 0,
    }


__all__ = [
    "brief",
    "collect",
    "comments",
    "credits",
    "daily",
    "endpoint_platforms",
    "endpoints",
    "estimate",
    "profiles",
    "scrapecreators_call",
    "search",
    "transcript",
    "user_posts",
    "wikipedia_pageviews",
]
