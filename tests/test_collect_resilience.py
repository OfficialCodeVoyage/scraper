"""A daily run survives an answer it cannot read: one bad source fails alone, what was paid for stays stored, and a
lens's budget counts the credits its calls cost."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from support import DAY, NOW, SC_HOST, FakeWeb

from scout import api
from scout.store import CreditLedger, Store

PROFILE = """
niche: test niche
comments: {top_posts: 0, youtube_videos: 0, hn_stories: 0}
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 5
    reddit:
      subreddits: [{name: good}, {name: odd}, {name: broken}]
    hn:
      queries: [bank run]
"""


def post(post_id: str, **fields: Any) -> dict[str, Any]:
    values = {
        "id": post_id,
        "title": f"post {post_id}",
        "permalink": f"/r/Sub/comments/{post_id}/x/",
        "subreddit": "Sub",
        "created_utc": NOW.timestamp() - 3600,
        "score": 500,
        "num_comments": 3,
    }
    return {**values, **fields}


def subreddit_answers(request: httpx.Request) -> httpx.Response:
    name = request.url.params["subreddit"]
    if name == "good":
        return httpx.Response(200, json={"posts": [post("g1"), post("g2")]})
    if name == "odd":
        # Not an object, a wrong-typed field, an out-of-range date and score: skipped or read as empty, never fatal.
        odd = [None, "text", post("o1", created_utc=float("inf"), score=1e30), post("o2", upvote_ratio="lots")]
        return httpx.Response(200, text=json.dumps({"posts": odd}), headers={"content-type": "application/json"})
    return httpx.Response(200, json={"posts": {"not": "a list"}})


@pytest.fixture
def run_setup(web: FakeWeb, with_key: str, add_profile: Callable[[str, str], Any]) -> None:
    add_profile("test", PROFILE)
    web.on(SC_HOST, "/v1/reddit/subreddit", handler=subreddit_answers)


async def test_odd_answers_are_skipped_element_by_element_and_the_run_keeps_what_it_paid_for(
    web: FakeWeb, run_setup: None, ledger: CreditLedger
) -> None:
    web.on("hn.algolia.com", "/api/v1/search_by_date", body={"hits": [None, {"objectID": "7", "title": "t"}]})

    run = await api.collect("test")

    assert run["status"] == "collected" and run["paid_calls"] == 3
    assert ledger.spent(DAY) == 3
    store = Store.for_profile("test")
    try:
        keys = sorted(i.key for i in store.items_of_day(DAY))
        odd = {i.ext_id: i for i in store.items_of_day(DAY) if i.ext_id in ("o1", "o2")}
    finally:
        store.close()
    assert keys == ["hn:7", "reddit:g1", "reddit:g2", "reddit:o1", "reddit:o2"]
    assert odd["o1"].created_at is None and odd["o1"].score == 2**53  # Infinity and 1e30 do not break the store
    assert odd["o2"].upvote_ratio is None


async def test_a_source_that_raises_something_unexpected_fails_alone_with_a_warning(
    web: FakeWeb, run_setup: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    web.on("hn.algolia.com", "/api/v1/search_by_date", body={"hits": []})

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("an answer nobody expected")

    monkeypatch.setattr("scout.keyless.KeylessSources.hn_search", boom)

    run = await api.collect("test")

    assert any(w.startswith("hn 'bank run':") for w in run["warnings"])
    store = Store.for_profile("test")
    try:
        assert {i.key for i in store.items_of_day(DAY)} >= {"reddit:g1", "reddit:g2"}
    finally:
        store.close()


async def test_a_lens_budget_counts_credits_not_calls(
    web: FakeWeb, with_key: str, add_profile: Callable[[str, str], Any], ledger: CreditLedger
) -> None:
    add_profile(
        "test",
        """
niche: test niche
comments: {top_posts: 0, youtube_videos: 0, hn_stories: 0}
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 4
    reddit:
      subreddits: [{name: one}, {name: two}, {name: three}, {name: four}]
""",
    )
    # Each answer says it cost 2 credits: a budget of 4 holds two calls, not four.
    web.on(SC_HOST, "/v1/reddit/subreddit", body={"posts": [], "credits_charged": 2})

    run = await api.collect("test", sources=["reddit"])

    assert (run["paid_calls"], run["skipped"]) == (2, 2)
    assert ledger.spent(DAY) == 4


async def test_a_post_both_lenses_found_has_its_comments_read_once(
    web: FakeWeb, with_key: str, add_profile: Callable[[str, str], Any], ledger: CreditLedger
) -> None:
    add_profile(
        "test",
        """
niche: test niche
comments: {top_posts: 5, youtube_videos: 0, hn_stories: 0}
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 1
    reddit: {subreddits: [{name: one}]}
  dev_intel:
    label: Dev
    credit_budget: 1
    reddit: {subreddits: [{name: two}]}
""",
    )
    web.on(SC_HOST, "/v1/reddit/subreddit", body={"posts": [post("shared", num_comments=40)]})
    web.on(SC_HOST, "/v1/reddit/post/comments", body={"comments": []})

    await api.collect("test", sources=["reddit"])

    assert [r.url.path for r in web.paid].count("/v1/reddit/post/comments") == 1
    assert ledger.spent(DAY) == 3
