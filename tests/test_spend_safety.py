"""What a call may cost and what the ledger believes: open-ended spend, odd answers, refused calls, the balance diff.

Open-ended spend (a paid option with no upper price) is the global config's call alone; an answer that is not what it
should be never makes a call free; a call the host refused costs nothing; the external-use diff counts scout's own
calls as scout's.
"""

from __future__ import annotations

from collections.abc import Callable
import httpx
import pytest
from support import DAY, FAKE_KEY, SC_HOST, FakeWeb

from scout import api
from scout.config import GlobalConfig
from scout.models import CreditCapReached, ProviderError, ScoutError
from scout.scrapecreators import ScrapeCreatorsClient, credit_count, endpoint
from scout.store import CreditLedger

MakeClient = Callable[..., ScrapeCreatorsClient]
TIKTOK = "/v1/tiktok/video/transcript"
VIDEO = "https://www.tiktok.com/@someone/video/1234567890"
LONG = {"url": VIDEO, "allow_long_transcription": "true"}


def open_ended_client(make_client: MakeClient, cap: int, limit: int | None) -> ScrapeCreatorsClient:
    client = make_client(cap)
    client._open_ended_limit = limit  # what the API passes from the global config
    return client


# H1: open-ended spend is config-only and bounded


async def test_no_argument_can_turn_on_open_ended_spend(
    web: FakeWeb, with_key: str, make_client: MakeClient, ledger: CreditLedger
) -> None:
    with pytest.raises(ScoutError, match="allow_open_ended_spend"):
        await make_client(100).call(TIKTOK, LONG, allow_paid_params=True)
    for dry_run in (True, False):
        with pytest.raises(ScoutError, match="allow_open_ended_spend"):
            await api.scrapecreators_call(TIKTOK, LONG, dry_run=dry_run, allow_paid_params=True)

    estimate = api.estimate(endpoint=TIKTOK, params=LONG)
    assert estimate["open_ended"] is True and estimate["refused"] is True
    assert web.requests == []
    assert ledger.spent(DAY) == 0


async def test_with_the_config_on_an_open_ended_call_reserves_the_configured_bound_and_settles_to_the_bill(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    reserved_when_sent: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        reserved_when_sent.append(ledger.spent(DAY))
        return httpx.Response(200, json={"transcript": "x", "credits_charged": 20})

    web.on(SC_HOST, TIKTOK, handler=answer)
    client = open_ended_client(make_client, 100, 30)

    with pytest.raises(ScoutError, match="allow_paid_params"):  # the config allows it; the call must still ask
        await client.call(TIKTOK, LONG)
    await client.call(TIKTOK, LONG, allow_paid_params=True)

    assert reserved_when_sent == [30]
    assert ledger.spent(DAY) == 20  # settled to what the answer says it cost
    assert web.paid[0].url.params["allow_long_transcription"] == "true"


async def test_an_open_ended_call_is_refused_when_less_than_the_bound_is_left(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body={"posts": []})
    client = open_ended_client(make_client, 30, 30)
    await client.reddit_search("bank run", "week")  # 29 left

    with pytest.raises(CreditCapReached, match="needs 30"):
        await client.call(TIKTOK, LONG, allow_paid_params=True)
    assert [r.url.path for r in web.paid] == ["/v1/reddit/search"]


def test_the_global_config_turns_open_ended_spend_on_and_bounds_it() -> None:
    assert GlobalConfig().open_ended_limit is None  # off by default
    assert GlobalConfig(allow_open_ended_spend=True).open_ended_limit == 30
    assert GlobalConfig(allow_open_ended_spend=True, max_credits_per_open_ended_call=50).open_ended_limit == 50
    with pytest.raises(ValueError, match="above the daily_credit_cap"):
        GlobalConfig(daily_credit_cap=20, allow_open_ended_spend=True, max_credits_per_open_ended_call=30)


# Hardening: a paid option set to an off value is dropped, not sent


async def test_a_paid_option_set_to_an_off_value_is_never_sent(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, TIKTOK, body={"transcript": "x"})
    client = make_client(100)
    for value in ("no", "false", "off", "0", ""):
        await client.call(TIKTOK, {"url": VIDEO, "allow_long_transcription": value})

    assert all("allow_long_transcription" not in r.url.params for r in web.paid)
    assert ledger.spent(DAY) == 5  # the flat price, one credit each
    assert endpoint(TIKTOK).check({"url": VIDEO, "allow_long_transcription": "No"}) == {"url": VIDEO}


# M1: an answer that is not what it should be keeps its reservation; a JSON list is an answer


async def test_a_200_that_is_not_json_keeps_its_reservation(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", text="<html>gateway hiccup</html>")
    client = make_client(10)

    with pytest.raises(ProviderError) as failed:
        await client.call("/v1/reddit/search", {"query": "bank run"})

    assert failed.value.status == 200
    assert ledger.spent(DAY) == 1
    assert client.credits_used == 1


async def test_a_json_list_answer_is_returned_and_charged(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    clip = [{"data": {"clip": {"id": "1", "title": "a clip"}}}]
    web.on(SC_HOST, "/v1/twitch/clip", body=clip)

    answer = await make_client(10).call("/v1/twitch/clip", {"url": "https://www.twitch.tv/a/clip/B"})

    assert answer == clip
    assert ledger.spent(DAY) == 1


async def test_a_typed_method_given_a_list_fails_with_its_credit_kept(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body=[{"id": "x"}])
    with pytest.raises(ProviderError, match="not an object"):
        await make_client(10).reddit_search("bank run", "week")
    assert ledger.spent(DAY) == 1


# L1: a 429 that asks for too long a wait was refused, not served


async def test_a_429_with_a_long_retry_after_releases_the_reservation(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, TIKTOK, status=429, headers={"retry-after": "60"})
    web.on(SC_HOST, "/v1/reddit/search", status=429, headers={"retry-after": "60"})
    client = open_ended_client(make_client, 100, 30)

    for path, params, paid in (("/v1/reddit/search", {"query": "a"}, False), (TIKTOK, LONG, True)):
        with pytest.raises(ProviderError) as failed:
            await client.call(path, params, allow_paid_params=paid)
        assert failed.value.status == 429

    assert len(web.paid) == 2
    assert ledger.spent(DAY) == 0
    assert client.credits_used == 0


# L3: an error body that echoes the key is masked where the error is made


async def test_an_error_body_that_echoes_the_key_is_masked_at_the_source(web: FakeWeb, make_client: MakeClient) -> None:
    web.on(SC_HOST, "/v1/reddit/search", status=401, body={"message": f"Invalid API key {FAKE_KEY}"})
    with pytest.raises(ProviderError) as failed:
        await make_client(10).call("/v1/reddit/search", {"query": "a"})
    assert FAKE_KEY not in str(failed.value) and "***" in str(failed.value)


# L4: only a non-negative number settles the ledger


def test_credit_counts_accept_only_non_negative_numbers() -> None:
    assert [credit_count(v) for v in (0, 3, "2", "2.0", 1.9, " 4 ")] == [0, 3, 2, 2, 2, 4]
    for bad in (-50, "-1", "free", None, True, float("inf"), float("nan"), [], {}, 10**12):
        assert credit_count(bad) is None, bad


async def test_a_negative_or_unreadable_charge_keeps_the_reservation(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    charges = iter([-50, "free", "2.0", -1, 1])
    web.on(
        SC_HOST,
        "/v1/reddit/search",
        handler=lambda r: httpx.Response(200, json={"posts": [], "credits_charged": next(charges)}),
    )
    client = make_client(3)
    for _ in range(2):  # -50 and "free": the reservation (1 each) stands
        await client.reddit_search("bank run", "week")
    assert ledger.spent(DAY) == 2
    await client.reddit_search("bank run", "week")  # "2.0" settles the third call to 2: 4 credits, above the cap of 3

    with pytest.raises(CreditCapReached):
        await client.reddit_search("bank run", "week")
    assert ledger.spent(DAY) == 4
    assert len(web.paid) == 3


# L5: scout's own calls are not "used elsewhere"


async def test_scouts_own_calls_without_credits_remaining_are_not_counted_as_used_elsewhere(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body={"posts": [], "credits_charged": 1})  # no credits_remaining
    client = make_client(10)

    assert ledger.record_balance(1000, DAY) == 0
    await client.reddit_search("bank run", "week")
    await client.reddit_search("bank run", "week")
    assert ledger.record_balance(998, DAY) == 0  # the two credits were scout's
    assert ledger.record_balance(995, DAY) == 3  # these three were not
    assert ledger.external(DAY) == 3


async def test_a_call_without_credits_remaining_does_not_inherit_an_older_one(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    answers = iter([{"posts": [], "credits_charged": 1, "credits_remaining": 500}, {"posts": []}])
    web.on(SC_HOST, "/v1/reddit/search", handler=lambda r: httpx.Response(200, json=next(answers)))
    client = make_client(10)
    await client.reddit_search("bank run", "week")
    await client.reddit_search("bank run", "week")

    rows = ledger._db.execute("SELECT remaining FROM credit_ledger ORDER BY id").fetchall()
    assert [row["remaining"] for row in rows] == [500, None]
    assert ledger.record_balance(499, DAY) == 0  # 500 less scout's second credit


async def test_a_large_account_balance_is_still_recorded(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body={"posts": [], "credits_charged": 1, "credits_remaining": 25_000_000})
    await make_client(10).reddit_search("bank run", "week")
    assert ledger.last_balance() == 25_000_000
