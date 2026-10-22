"""The hard daily credit cap: credits are reserved before a request is sent, and a call the cap refuses is never sent.

Most tests drive ``ScrapeCreatorsClient`` on the fake web; the ones about "one ledger for everything" and dry runs go
through ``scout.api``, the same functions the CLI and the MCP server call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from support import DAY, SC_HOST, FakeWeb
from scout import api
from scout.models import CreditCapReached, ProviderError
from scout.scrapecreators import ScrapeCreatorsClient
from scout.store import CreditLedger

MakeClient = Callable[..., ScrapeCreatorsClient]

# Lens budgets 2 and 1 over six subreddits: three calls fit, three do not. No comment calls, no free sources.
TWO_LENS_PROFILE = """
niche: test niche
comments: {top_posts: 0, youtube_videos: 0, hn_stories: 0}
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 2
    reddit:
      subreddits: [{name: one}, {name: two}, {name: three}, {name: four}]
  dev_intel:
    label: Dev
    credit_budget: 1
    reddit:
      subreddits: [{name: five}, {name: six}]
"""


@pytest.fixture
def reddit(web: FakeWeb, load: Callable[[str], Any]) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body=load("scrapecreators/reddit_search"))
    web.on(SC_HOST, "/v1/reddit/subreddit", body=load("scrapecreators/reddit_subreddit"))


async def test_a_credit_is_reserved_in_the_ledger_before_the_request_is_sent(
    web: FakeWeb, reddit: None, make_client: MakeClient, ledger: CreditLedger
) -> None:
    spent_when_sent: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        spent_when_sent.append(ledger.spent(DAY))
        return httpx.Response(200, json={"posts": []})

    web.on(SC_HOST, "/v1/reddit/search", handler=answer)
    await make_client(5).reddit_search("bank run", "week")
    assert spent_when_sent == [1]


async def test_with_a_cap_of_n_the_next_call_is_refused_and_never_sent(
    web: FakeWeb, reddit: None, make_client: MakeClient, ledger: CreditLedger
) -> None:
    cap = 3
    client = make_client(cap)
    for _ in range(cap):
        await client.reddit_search("bank run", "week")

    with pytest.raises(CreditCapReached, match=f"{cap} of {cap}"):
        await client.reddit_search("bank run", "week")

    assert len(web.paid) == cap
    assert ledger.spent(DAY) == cap
    assert ledger.left(DAY, cap) == 0
    assert (client.calls, client.credits_used) == (cap, cap)


async def test_two_clients_on_one_ledger_file_share_one_cap(
    web: FakeWeb, reddit: None, make_client: MakeClient
) -> None:
    first, second = make_client(2), make_client(2)  # two connections to the same credits.db
    await first.reddit_search("bank run", "week")
    await second.reddit_search("bank run", "week")

    with pytest.raises(CreditCapReached):
        await first.reddit_search("bank run", "week")
    with pytest.raises(CreditCapReached):
        await second.reddit_search("bank run", "week")
    assert len(web.paid) == 2


async def test_the_cap_counts_per_calendar_day(web: FakeWeb, reddit: None, make_client: MakeClient) -> None:
    today, tomorrow = make_client(1, day="2026-10-20"), make_client(1, day="2026-10-21")
    await today.reddit_search("bank run", "week")
    with pytest.raises(CreditCapReached):
        await today.reddit_search("bank run", "week")

    await tomorrow.reddit_search("bank run", "week")
    assert len(web.paid) == 2


async def test_profile_runs_and_ad_hoc_calls_draw_on_the_same_ledger(
    web: FakeWeb,
    reddit: None,
    with_key: str,
    set_cap: Callable[[int], None],
    add_profile: Callable[[str, str], Any],
    ledger: CreditLedger,
) -> None:
    set_cap(4)
    add_profile("test", TWO_LENS_PROFILE)

    run = await api.collect("test", sources=["reddit"])  # the lens budgets let it make 3 calls
    assert run["paid_calls"] == 3

    last_credit = await api.search("reddit", "bank run")  # the 4th credit of the day
    assert last_credit["count"] == 2
    assert last_credit["credits_left_today"] == 0

    refused_search = await api.search("reddit", "bank run")
    refused_call = await api.scrapecreators_call("/v1/reddit/search", {"query": "bank run"})
    for refused in (refused_search, refused_call):
        assert refused["skipped"] is True
        assert "Credit cap" in refused["reason"]
        assert refused["credits_used"] == 0
    assert refused_search["items"] == []
    assert refused_call["response"] is None

    assert len(web.paid) == 4
    assert ledger.spent(DAY) == 4


async def test_a_lens_stops_at_its_credit_budget_while_the_other_lens_keeps_its_own(
    web: FakeWeb,
    reddit: None,
    with_key: str,
    add_profile: Callable[[str, str], Any],
    ledger: CreditLedger,
) -> None:
    add_profile("test", TWO_LENS_PROFILE)

    run = await api.collect("test", sources=["reddit"])

    assert (run["paid_calls"], run["skipped"]) == (3, 3)
    assert [r.url.params["subreddit"] for r in web.paid] == ["one", "two", "five"]
    assert ledger.spent(DAY) == 3


async def test_dry_run_sends_nothing_and_reserves_nothing_even_with_a_key(
    web: FakeWeb, with_key: str, add_profile: Callable[[str, str], Any], ledger: CreditLedger
) -> None:
    add_profile("test", TWO_LENS_PROFILE)

    search = await api.search("reddit", "bank run", dry_run=True)
    call = await api.scrapecreators_call("/v1/reddit/search", {"query": "bank run"}, dry_run=True)
    run = await api.collect("test", dry_run=True)

    assert (search["dry_run"], search["credits_needed"], search["credits_used"]) == (True, 1, 0)
    assert (call["dry_run"], call["credits_needed"], call["credits_used"]) == (True, 1, 0)
    assert (run["dry_run"], run["paid_calls"], run["credits_used"]) == (True, 6, 0)
    assert web.requests == []  # not even the free balance check
    assert ledger.endpoint_use(DAY) == []
    assert ledger.spent(DAY) == 0


# What a failed call costs: the code's rule is that a 4xx answer releases the reservation (the call was refused, not
# served) while a 5xx answer or no answer at all keeps it (the provider may have done the work).


async def test_a_client_error_answer_releases_the_reservation(
    web: FakeWeb, reddit: None, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", status=404, body={"message": "no such thing"})
    client = make_client(1)

    with pytest.raises(ProviderError) as failed:
        await client.reddit_search("bank run", "week")

    assert failed.value.status == 404
    assert ledger.spent(DAY) == 0
    assert client.credits_used == 0
    web.on(SC_HOST, "/v1/reddit/search", body={"posts": []})  # a later route wins
    await client.reddit_search("bank run", "week")  # the one credit of the cap is still there
    assert len(web.paid) == 2


async def test_a_server_error_answer_keeps_the_reservation(
    web: FakeWeb, reddit: None, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", status=500, body={"message": "upstream broke"})
    client = make_client(1)

    with pytest.raises(ProviderError) as failed:
        await client.reddit_search("bank run", "week")
    assert failed.value.status == 500
    assert ledger.spent(DAY) == 1
    assert client.credits_used == 1

    with pytest.raises(CreditCapReached):
        await client.reddit_search("bank run", "week")
    assert len(web.paid) == 1


async def test_a_connection_failure_keeps_the_reservation(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    web.on(SC_HOST, "/v1/reddit/search", handler=refuse)
    client = make_client(1)

    with pytest.raises(ProviderError) as failed:
        await client.reddit_search("bank run", "week")

    assert failed.value.status is None
    assert ledger.spent(DAY) == 1
    assert client.credits_used == 1


async def test_the_ledger_settles_to_the_cost_the_answer_reports(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    client = make_client(10)
    answers = [
        ({"credits_charged": 0, "credits_remaining": 500, "posts": []}, 0),  # a cached answer is free
        ({"credits_charged": 3, "credits_remaining": 497, "posts": []}, 3),  # what the API says it charged
        ({"posts": []}, 1),  # no cost reported: the allowlist's cost
    ]
    spent = 0
    for body, cost in answers:
        web.on(SC_HOST, "/v1/reddit/search", body=body)
        await client.reddit_search("bank run", "week")
        spent += cost
        assert ledger.spent(DAY) == spent

    assert client.credits_used == spent
    assert ledger.last_balance() == 497
