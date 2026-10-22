"""The free sources, no key and no account: Hacker News (Algolia), GitHub search, Bluesky and Wikipedia pageviews.

Limits: GitHub's search allows an anonymous caller 10 requests a minute (kept at 9 here); Algolia's HN API and
Bluesky's public AppView (``api.bsky.app``; ``public.api.bsky.app`` refuses search) publish none, so a modest client
limit is used; Wikimedia asks for a User-Agent naming the client. Checked 2026-10-19/20.
"""

from __future__ import annotations

import html
import math
import re
import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any, Self
from urllib.parse import quote

import httpx

from scout.http import build_http_client, describe_error, limited_transport
from scout.models import Comment, Item, ProviderError
from scout.parsing import as_int, epoch, field, objects, parsed
from scout.parsing import iso as _iso

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_SEARCH_RELEVANCE = "https://hn.algolia.com/api/v1/search"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
GITHUB_SEARCH = "https://api.github.com/search/repositories"
BLUESKY_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
BLUESKY_AUTHOR_FEED = "https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
WIKI_OPENSEARCH = "https://en.wikipedia.org/w/api.php"
WIKI_PAGEVIEWS = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{title}/daily/"
    "{start}/{end}"
)
_TAG = re.compile(r"<[^>]+>")


def _plain(markup: str | None) -> str:
    return html.unescape(_TAG.sub(" ", markup or "")).strip()


def _client(user_agent: str, per_minute: int, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return build_http_client(user_agent=user_agent, timeout=30.0, transport=limited_transport(per_minute, transport))


class KeylessSources:
    """One client per host for a run; ``async with`` closes them."""

    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._hn = _client(user_agent, 60, transport)
        self._github = _client(user_agent, 9, transport)
        self._bluesky = _client(user_agent, 30, transport)
        self._wiki = _client(user_agent, 60, transport)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        for client in (self._hn, self._github, self._bluesky, self._wiki):
            await client.aclose()

    @staticmethod
    async def _json(client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            raise ProviderError(f"GET {url} failed: {describe_error(exc)}", status=status) from exc
        except ValueError as exc:
            raise ProviderError(f"GET {url} returned invalid JSON") from exc

    async def hn_search(
        self,
        query: str,
        min_points: int,
        since: datetime | None,
        lens: str = "",
        *,
        limit: int = 30,
        ad_hoc: bool = False,
    ) -> list[Item]:
        """Stories matching ``query``. The daily run searches titles only, newest first. An ``ad_hoc`` search ranks
        by relevance across titles and story text with typo tolerance off instead."""
        filters = [f"points>{min_points}"]
        if since is not None:
            filters.insert(0, f"created_at_i>{int(since.timestamp())}")
        params = {"query": query, "tags": "story", "numericFilters": ",".join(filters), "hitsPerPage": str(limit)}
        if ad_hoc:
            params["typoTolerance"] = "false"
        else:
            # Algolia is typo-tolerant across the story text: "MCP" matched "GCP" until it was titles only.
            params["restrictSearchableAttributes"] = "title"
        body = await self._json(self._hn, HN_SEARCH_RELEVANCE if ad_hoc else HN_SEARCH, params)

        def one(hit: dict[str, Any]) -> Item | None:
            if not hit.get("objectID"):
                return None
            link = hit.get("url")
            return Item(
                source="hn",
                ext_id=str(hit["objectID"]),
                url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                title=str(hit.get("title") or ""),
                text=_plain(str(hit.get("story_text") or ""))[:4000],
                author=str(hit.get("author") or ""),
                community="hn",
                created_at=epoch(hit.get("created_at_i")) if hit.get("created_at_i") else None,
                score=as_int(hit.get("points")),
                comments=as_int(hit.get("num_comments")),
                lens=lens,
                watch=f"hn: {query}",
                link=link if isinstance(link, str) else None,
            )

        return parsed(objects(field(body, "hits")), one, "Hacker News story")

    async def hn_comments(self, item: Item, limit: int = 20) -> list[Comment]:
        return await self.hn_comments_of(item.ext_id, limit)

    async def hn_comments_of(self, story_id: str, limit: int = 20) -> list[Comment]:
        body = await self._json(self._hn, HN_ITEM.format(id=quote(story_id, safe="")))

        def one(child: dict[str, Any]) -> Comment | None:
            text = _plain(str(child.get("text") or ""))
            if not text:
                return None
            return Comment(
                source="hn",
                post_id=story_id,
                ext_id=str(child.get("id")),
                author=str(child.get("author") or ""),
                text=text[:1500],
                score=0,
                created_at=_iso(child.get("created_at")),
            )

        return parsed(objects(field(body, "children"))[:limit], one, "Hacker News comment")

    async def github_search(
        self, query: str, min_stars: int | None, since: date | None, lens: str = "", *, limit: int = 20
    ) -> list[Item]:
        qualifiers = [query]
        if since is not None:
            qualifiers.append(f"created:>{since.isoformat()}")
        if min_stars is not None:
            qualifiers.append(f"stars:>{min_stars}")
        body = await self._json(
            self._github,
            GITHUB_SEARCH,
            {"q": " ".join(qualifiers), "sort": "stars", "order": "desc", "per_page": str(limit)},
        )

        def one(repo: dict[str, Any]) -> Item | None:
            if not repo.get("full_name"):
                return None
            homepage = repo.get("homepage")
            return Item(
                source="github",
                ext_id=str(repo["full_name"]),
                url=str(repo.get("html_url") or ""),
                title=str(repo["full_name"]),
                text=str(repo.get("description") or "")[:1000],
                author=str((repo.get("owner") or {}).get("login") or ""),
                community="github",
                created_at=_iso(repo.get("created_at")),
                score=as_int(repo.get("stargazers_count")),
                comments=0,
                lens=lens,
                watch=f"github: {query}",
                link=homepage if isinstance(homepage, str) and homepage else None,
                extra={
                    "language": repo.get("language"),
                    "topics": repo.get("topics") or [],
                    "licence": (repo.get("license") or {}).get("spdx_id"),
                },
            )

        return parsed(objects(field(body, "items")), one, "GitHub repository")

    @staticmethod
    def _bluesky_item(post: dict[str, Any], lens: str, watch: str) -> Item | None:
        uri = str(post.get("uri") or "")
        handle = str((post.get("author") or {}).get("handle") or "")
        record = post.get("record") or {}
        text = str(record.get("text") or "")[:4000]
        if not uri or not handle:
            return None
        return Item(
            source="bluesky",
            ext_id=uri,
            url=f"https://bsky.app/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}",
            title=text.strip().splitlines()[0][:140] if text.strip() else "",
            text=text,
            author=handle,
            community="bluesky",
            created_at=_iso(record.get("createdAt")),
            score=as_int(post.get("likeCount")),
            comments=as_int(post.get("replyCount")),
            lens=lens,
            watch=watch,
            extra={
                "reposts": as_int(post.get("repostCount")),
                "quotes": as_int(post.get("quoteCount")),
            },
        )

    async def bluesky_search(
        self, query: str, since: datetime | None, lens: str = "", *, limit: int = 25, sort: str = "top"
    ) -> list[Item]:
        body = await self._json(
            self._bluesky,
            BLUESKY_SEARCH,
            {
                "q": query,
                "sort": sort,
                **({"since": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")} if since else {}),
                "limit": str(max(1, min(limit, 100))),
            },
        )
        watch = f"bluesky: {query}"
        return parsed(objects(field(body, "posts")), lambda p: self._bluesky_item(p, lens, watch), "Bluesky post")

    async def bluesky_author_feed(self, handle: str, lens: str = "", *, limit: int = 25) -> list[Item]:
        """An account's own posts (no reposts), newest first."""
        actor = handle.strip().lstrip("@")
        body = await self._json(
            self._bluesky,
            BLUESKY_AUTHOR_FEED,
            {"actor": actor, "limit": str(max(1, min(limit, 100))), "filter": "posts_no_replies"},
        )
        watch = f"bluesky: @{actor}"
        return parsed(
            objects(field(body, "feed")),
            lambda entry: None if entry.get("reason") else self._bluesky_item(entry.get("post") or {}, lens, watch),
            "Bluesky post",
        )

    async def wikipedia_pageviews(self, article: str, start: date, end: date) -> dict[str, Any]:
        """Daily page views of the English Wikipedia ``article`` (a title, spaces or underscores) from start to end.

        Raises:
            ProviderError: Wikipedia has no such article (status 404) or the call failed.
        """
        title = article.strip().replace(" ", "_")
        body = await self._json(
            self._wiki,
            WIKI_PAGEVIEWS.format(
                title=quote(title, safe=""), start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")
            ),
        )

        def day_row(row: dict[str, Any]) -> dict[str, Any]:
            stamp = str(row.get("timestamp"))
            return {"date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}", "views": as_int(row.get("views"))}

        days = parsed(objects(field(body, "items")), day_row, "page-view day")
        views = [d["views"] for d in days]
        return {
            "article": title,
            "url": f"https://en.wikipedia.org/wiki/{quote(title)}",
            "days": days,
            "total": sum(views),
            "daily_mean": round(statistics.fmean(views)) if views else 0,
        }

    async def wikipedia_spike(self, name: str, day: date) -> dict[str, Any] | None:
        """How unusual the last 7 days of page views of ``name`` are against the 83 days before: a z-score.

        None when Wikipedia has no page for it or too little history.
        """
        found = await self._json(
            self._wiki,
            WIKI_OPENSEARCH,
            {
                "action": "opensearch",
                "search": name,
                "limit": "1",
                "namespace": "0",
                "redirects": "resolve",
                "format": "json",
            },
        )
        titles = found[1] if isinstance(found, list) and len(found) > 1 else []
        if not titles:
            return None
        title = str(titles[0])
        # Opensearch falls back to a fuzzy match: "Evan Hafer", who has no page, came back as "Evan Ferguson".
        asked, got = name.strip().casefold(), title.casefold()
        if not (got.startswith(asked) or asked.startswith(got)):
            return None
        end = day - timedelta(days=1)
        start = end - timedelta(days=89)
        try:
            body = await self._json(
                self._wiki,
                WIKI_PAGEVIEWS.format(
                    title=quote(title.replace(" ", "_"), safe=""),
                    start=start.strftime("%Y%m%d"),
                    end=end.strftime("%Y%m%d"),
                ),
            )
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        views = [as_int(row.get("views")) for row in objects(field(body, "items"))]
        if len(views) < 30:
            return None
        base, recent = views[:-7], views[-7:]
        spread = statistics.pstdev(base) or 1.0
        recent_mean = statistics.fmean(recent)
        return {
            "title": title,
            "url": f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
            "z": round((recent_mean - statistics.fmean(base)) / spread, 1),
            "recent_daily": round(recent_mean),
            "baseline_daily": round(statistics.fmean(base)),
            "log_views": round(math.log10(max(recent_mean, 1)), 1),
        }
