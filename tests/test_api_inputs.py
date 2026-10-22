"""What the API functions accept before they spend: source names, URL shapes, and the window ``since_days`` asks for."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from support import NOW, SC_HOST, FakeWeb

from scout import api
from scout.models import ScoutError
from scout.scrapecreators import x_months, x_query
from scout.store import CreditLedger

PROFILE = """
niche: test niche
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 10
    reddit:
      subreddits: [{name: a}, {name: b}]
    x:
      searches: [q1, q2]
"""


# M4: estimate and the run read source names the same way


def test_estimate_reads_source_names_like_the_run_does(add_profile: Callable[[str, str], Any]) -> None:
    add_profile("p", PROFILE)
    for names in (["reddit", "x"], ["Reddit", "twitter"], [" X ", "REDDIT"]):
        estimate = api.estimate(profile="p", sources=names)
        plan = asyncio.run(api.daily("p", sources=names, dry_run=True))
        assert estimate["paid_credits"] == plan["paid_credits"] == 4, names
    with pytest.raises(ScoutError, match="unknown source"):
        api.estimate(profile="p", sources=["myspace"])


# L9: a Reddit URL that is not a post is refused before anything is reserved


async def test_a_reddit_url_that_is_not_a_post_is_refused_before_it_costs_anything(
    web: FakeWeb, with_key: str, ledger: CreditLedger
) -> None:
    for url in (
        "https://www.reddit.com/r/Sub/",
        "https://www.reddit.com/user/someone",
        "https://www.reddit.com/r/Sub/s/AbCdEf",
        "https://www.reddit.com/",
    ):
        with pytest.raises(ScoutError, match="not a Reddit post URL"):
            await api.comments(url)
        with pytest.raises(ScoutError, match="not a Reddit post URL"):
            await api.transcript(url)
    assert web.requests == []
    assert ledger.endpoint_use(NOW.date().isoformat()) == []

    for url, post_id in (
        ("https://www.reddit.com/r/Sub/comments/abc123/a_title/", "abc123"),
        ("https://old.reddit.com/r/Sub/comments/abc123", "abc123"),
        ("https://redd.it/abc123", "abc123"),
    ):
        assert api._url_source(url) == ("reddit", post_id)


# L7: since_days is a real window on the paid sources


async def test_paid_search_results_are_cut_to_since_days_and_undated_ones_are_kept_and_marked(
    web: FakeWeb, with_key: str
) -> None:
    def post(post_id: str, hours_old: float | None) -> dict[str, Any]:
        created = None if hours_old is None else (NOW - timedelta(hours=hours_old)).timestamp()
        return {"id": post_id, "title": post_id, "permalink": f"/r/s/comments/{post_id}/x/", "created_utc": created}

    posts = [post("new", 2), post("old", 24 * 6), post("undated", None)]
    web.on(SC_HOST, "/v1/reddit/search", body={"posts": posts})

    found = await api.search("reddit", "bank run", since_days=1)

    assert [i["id"] for i in found["items"]] == ["new", "undated"]
    assert found["older_dropped"] == 1
    assert found["items"][1]["extra"]["undated"] is True
    assert "undated" not in found["items"][0]["extra"]


def test_x_search_adds_every_month_its_window_touches() -> None:
    assert x_months("2026-10-20", 7) == ["October 2026"]
    assert x_months("2026-10-03", 7) == ["October 2026", "September 2026"]
    assert x_months("2026-03-01", 31) == ["March 2026", "February 2026", "January 2026"]
    assert x_months("2026-01-02", 7) == ["January 2026", "December 2025"]
    assert x_months("2026-10-20", None) == x_months("2026-10-20", 32) == []

    assert x_query("bank run", ["October 2026"]) == "bank run October 2026"
    assert x_query("bank run", ["October 2026", "September 2026"]) == 'bank run "October 2026" OR "September 2026"'
    assert x_query("bank run", []) == "bank run"


async def test_an_early_month_x_search_asks_for_the_previous_month_too(web: FakeWeb, with_key: str) -> None:
    web.on(SC_HOST, "/v1/twitter/search/posts", body={"posts": []})
    await api.search("x", "bank run", since_days=30)  # the API's clock is 2026-10-20: the window starts in September
    plan = await api.search("x", "bank run", since_days=30, dry_run=True)

    sent = 'bank run "October 2026" OR "September 2026"'
    assert web.paid[0].url.params["query"] == sent
    assert plan["calls"][0]["params"]["query"] == sent
