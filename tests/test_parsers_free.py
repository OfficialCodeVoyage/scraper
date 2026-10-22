"""The free sources (Hacker News, GitHub, Bluesky, Wikipedia) are normalised to the same item shape.

The fixtures under ``tests/fixtures/free`` are small synthetic answers in each service's documented shape, with
invented names: no real account or post is copied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import date, timedelta
from typing import Any

import pytest
from support import NOW, USER_AGENT, FakeWeb, check_shape

from scout import api
from scout.keyless import KeylessSources
from scout.models import ProviderError

Load = Callable[[str], Any]
HN = "hn.algolia.com"
GITHUB = "api.github.com"
BLUESKY = "api.bsky.app"


@pytest.fixture
async def free(web: FakeWeb) -> AsyncIterator[KeylessSources]:
    async with KeylessSources(USER_AGENT, transport=web.transport) as sources:
        yield sources


async def test_hn_search_gives_stories_with_plain_text_and_the_outbound_link(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(HN, "/api/v1/search", body=load("free/hn_search"))

    items = await free.hn_search("bank run", 20, NOW - timedelta(days=3), "dev_intel")

    assert [i.ext_id for i in items] == ["9000001", "9000002"]  # the hit without an id is dropped
    first = check_shape(items[0], "hn")
    assert first["url"] == "https://news.ycombinator.com/item?id=9000001"
    assert first["title"] == "Example Corp files for bankruptcy"
    assert " ".join(first["text"].split()) == "Some & text with a tag"  # the markup is gone
    assert (first["author"], first["score"], first["comments"]) == ("fake_hn_user", 321, 88)
    assert first["created_at"] == "2026-09-21T14:13:20+00:00"
    assert first["link"] == "https://example.com/news/example-corp"
    assert items[1].link is None and items[1].text == ""  # an Ask HN has no link and no text
    assert (items[0].lens, items[0].watch) == ("dev_intel", "hn: bank run")


async def test_hn_daily_search_reads_titles_newest_first_and_an_ad_hoc_search_ranks_by_relevance(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(HN, "/api/v1/search", body=load("free/hn_search"))
    since = NOW - timedelta(days=3)

    await free.hn_search("MCP", 20, since)
    await free.hn_search("MCP", -1, since, ad_hoc=True, limit=5)

    daily, ad_hoc = web.requests
    assert daily.url.path == "/api/v1/search_by_date"
    assert daily.url.params["restrictSearchableAttributes"] == "title"  # Algolia's typo tolerance matched MCP to GCP
    assert daily.url.params["numericFilters"] == f"created_at_i>{int(since.timestamp())},points>20"
    assert ad_hoc.url.path == "/api/v1/search"
    assert ad_hoc.url.params["typoTolerance"] == "false"
    assert "restrictSearchableAttributes" not in ad_hoc.url.params
    assert ad_hoc.url.params["hitsPerPage"] == "5"


async def test_hn_comments_are_plain_text_and_empty_ones_are_skipped(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(HN, "/api/v1/items/", body=load("free/hn_item"))

    comments = await free.hn_comments_of("9000001")

    assert [c.ext_id for c in comments] == ["9100001", "9100003"]  # the deleted one has no text
    first = comments[0].to_dict()
    assert " ".join(first.pop("text").split()) == "First comment & more"  # the markup is gone
    assert first == {
        "source": "hn",
        "post_id": "9000001",
        "id": "9100001",
        "author": "fake_commenter_one",
        "score": 0,
        "created_at": "2026-09-21T15:00:00+00:00",
    }
    assert web.requests[0].url.path == "/api/v1/items/9000001"


async def test_github_search_gives_repositories_with_stars_as_score(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(GITHUB, "/search/repositories", body=load("free/github_search"))

    items = await free.github_search("video", 30, date(2026, 10, 13), "dev_intel", limit=20)

    assert [i.ext_id for i in items] == ["fake-org/example-repo", "fake-org/quiet-repo"]  # no full_name: dropped
    assert dict(web.requests[0].url.params) == {
        "q": "video created:>2026-10-13 stars:>30",
        "sort": "stars",
        "order": "desc",
        "per_page": "20",
    }
    first = check_shape(items[0], "github")
    assert first["url"] == "https://github.com/fake-org/example-repo"
    assert first["title"] == "fake-org/example-repo"
    assert (first["author"], first["score"], first["comments"]) == ("fake-org", 456, 0)
    assert first["link"] == "https://example.org/docs"
    assert first["extra"] == {"language": "Python", "topics": ["video", "example"], "licence": "MIT"}
    quiet = check_shape(items[1], "github")
    assert (quiet["text"], quiet["link"]) == ("", None)  # no description, an empty homepage
    assert quiet["extra"] == {"language": None, "topics": [], "licence": None}


async def test_bluesky_search_gives_posts_with_likes_as_score_and_a_web_link(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(BLUESKY, "/xrpc/app.bsky.feed.searchPosts", body=load("free/bluesky_search"))

    items = await free.bluesky_search("example", NOW - timedelta(days=3), limit=500)

    assert [i.ext_id for i in items] == ["at://did:plc:fakefakefakefake/app.bsky.feed.post/3kexample001"]
    data = check_shape(items[0], "bluesky")
    assert data["url"] == "https://bsky.app/profile/fake-user.example.test/post/3kexample001"
    assert data["title"] == "First line of an example post"
    assert (data["author"], data["score"], data["comments"]) == ("fake-user.example.test", 40, 3)
    assert data["extra"] == {"reposts": 5, "quotes": 1}
    assert data["created_at"] == "2026-10-01T12:00:00+00:00"
    params = web.requests[0].url.params
    assert (params["q"], params["sort"], params["limit"]) == ("example", "top", "100")  # the limit is clamped
    assert params["since"] == "2026-10-17T12:00:00Z"


async def test_a_bluesky_author_feed_leaves_out_posts_the_account_only_reposted(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on(BLUESKY, "/xrpc/app.bsky.feed.getAuthorFeed", body=load("free/bluesky_author_feed"))

    items = await free.bluesky_author_feed("@fake-user.example.test", limit=10)

    assert [i.title for i in items] == ["An original post by the account"]
    assert check_shape(items[0], "bluesky")["author"] == "fake-user.example.test"
    params = web.requests[0].url.params
    assert (params["actor"], params["filter"]) == ("fake-user.example.test", "posts_no_replies")  # no @


async def test_wikipedia_pageviews_cover_the_last_full_days_and_give_a_total_and_a_mean(
    web: FakeWeb, load: Load
) -> None:
    web.on("wikimedia.org", "/api/rest_v1/metrics/pageviews/", body=load("free/wikipedia_pageviews"))

    result = await api.wikipedia_pageviews("Example Corp", days=3)

    assert web.requests[0].url.path.endswith("/user/Example_Corp/daily/20261017/20261019")  # yesterday is the last day
    assert (result["start"], result["end"], result["count"]) == ("2026-10-17", "2026-10-19", 3)
    assert result["items"] == [
        {"date": "2026-10-17", "views": 100},
        {"date": "2026-10-18", "views": 200},
        {"date": "2026-10-19", "views": 300},
    ]
    assert (result["total"], result["daily_mean"], result["credits_used"]) == (600, 200, 0)
    assert result["article"] == "Example_Corp"


async def test_a_wikipedia_spike_is_the_last_week_against_the_83_days_before(
    web: FakeWeb, load: Load, free: KeylessSources
) -> None:
    web.on("en.wikipedia.org", "/w/api.php", body=load("free/wikipedia_opensearch"))
    start = date(2026, 10, 19) - timedelta(days=89)
    views = [90 if n % 2 == 0 else 110 for n in range(83)] + [1000] * 7
    rows = [{"timestamp": f"{start + timedelta(days=n):%Y%m%d}00", "views": count} for n, count in enumerate(views)]
    web.on("wikimedia.org", "/api/rest_v1/metrics/pageviews/", body={"items": rows})

    spike = await free.wikipedia_spike("Example Corp", date(2026, 10, 20))

    assert spike is not None
    assert spike["title"] == "Example Corp"
    assert (spike["recent_daily"], spike["baseline_daily"], spike["log_views"]) == (1000, 100, 3.0)
    assert spike["z"] > 50  # a thousand a day against 100 +- 10


async def test_a_wikipedia_spike_ignores_a_fuzzy_title_match(web: FakeWeb, load: Load, free: KeylessSources) -> None:
    # Opensearch falls back to a fuzzy match when there is no page of that name; asking for it must find nothing.
    web.on("en.wikipedia.org", "/w/api.php", body=load("free/wikipedia_opensearch"))

    assert await free.wikipedia_spike("Zzz Unrelated Name", date(2026, 10, 20)) is None
    assert web.sent_to("wikimedia.org") == []


async def test_a_free_source_that_fails_raises_a_provider_error_with_the_status(web: FakeWeb) -> None:
    web.on(HN, "/api/v1/search", status=500, body={"message": "broken"})
    with pytest.raises(ProviderError) as server_error:
        await api.search("hn", "bank run")
    assert server_error.value.status == 500

    web.on(HN, "/api/v1/search", text="<html>this is not json</html>")
    with pytest.raises(ProviderError, match="invalid JSON"):
        await api.search("hn", "bank run")

    web.on("wikimedia.org", "/api/rest_v1/metrics/pageviews/", status=404, body={"detail": "not found"})
    with pytest.raises(ProviderError) as missing:
        await api.wikipedia_pageviews("An Article That Does Not Exist")
    assert missing.value.status == 404
