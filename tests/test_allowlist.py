"""Only allowlisted ScrapeCreators endpoints can be called, and only with their documented parameters."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from support import DAY, FakeWeb

from scout import api
from scout.models import ScoutError
from scout.scrapecreators import BALANCE_PATH, ScrapeCreatorsClient, endpoint, load_endpoints
from scout.store import CreditLedger

MakeClient = Callable[..., ScrapeCreatorsClient]

# Every endpoint the typed methods (``reddit_search`` and the others) send to.
USED_BY_SCOUT = [
    "/v1/reddit/subreddit",
    "/v1/reddit/search",
    "/v1/reddit/post/comments",
    "/v1/youtube/channel-videos",
    "/v1/youtube/search",
    "/v1/youtube/search/typeahead",
    "/v1/youtube/video/comments",
    "/v1/youtube/video/transcript",
    "/v1/twitter/search/posts",
    "/v1/twitter/user-tweets",
    "/v1/threads/search",
    "/v1/threads/user/posts",
]


async def test_an_endpoint_that_is_not_allowlisted_is_refused_and_never_sent(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    client = make_client(10)
    unlisted = [
        "/v1/not/on/the/list",
        "/v1/account/get-api-usage",
        "/v1/reddit/search/../../account/credit-balance",  # a path that walks out of an allowlisted one
        "/v1/reddit/search?query=x&trim=true",  # a query string smuggled into the path
        "https://other.example.test/v1/reddit/search",  # another host
    ]
    for path in unlisted:
        with pytest.raises(ScoutError, match="allowlist"):
            await client.call(path, {"query": "x"})

    assert web.requests == []
    assert ledger.spent(DAY) == 0


async def test_a_missing_required_parameter_or_an_unknown_one_is_refused_and_never_sent(
    web: FakeWeb, make_client: MakeClient, ledger: CreditLedger
) -> None:
    client = make_client(10)
    for path, params in [
        ("/v1/reddit/search", {}),
        ("/v1/reddit/search", {"query": "   "}),  # blank is missing
        ("/v1/youtube/video/transcript", {"language": "en"}),  # the url is required
        ("/v1/twitter/user-tweets", {}),
    ]:
        with pytest.raises(ScoutError, match="needs"):
            await client.call(path, params)
    for path, params in [
        ("/v1/reddit/search", {"query": "x", "subreddit": "y"}),  # not a parameter of this endpoint
        ("/v1/reddit/search", {"query": "x", "Query": "y"}),
        ("/v1/credit-balance", {"anything": "1"}),
    ]:
        with pytest.raises(ScoutError, match="takes no parameter"):
            await client.call(path, params)

    assert web.requests == []
    assert ledger.spent(DAY) == 0


async def test_the_generic_call_refuses_bad_input_before_it_asks_for_a_key_or_opens_the_ledger(
    web: FakeWeb, ledger: CreditLedger
) -> None:
    # No key is set in this test: a MissingKeyError would mean the allowlist check came too late.
    with pytest.raises(ScoutError, match="allowlist"):
        await api.scrapecreators_call("/v1/not/on/the/list", {})
    with pytest.raises(ScoutError, match="needs query"):
        await api.scrapecreators_call("/v1/reddit/search", {})
    with pytest.raises(ScoutError, match="takes no parameter"):
        await api.scrapecreators_call("/v1/reddit/search", {"query": "x", "extra": "y"})

    assert web.requests == []
    assert ledger.endpoint_use(DAY) == []


def test_the_allowlist_is_well_formed_and_holds_every_endpoint_scout_uses() -> None:
    entries = load_endpoints()
    assert entries
    for path, entry in entries.items():
        assert path.startswith("/v") and ".." not in path and "?" not in path
        assert entry.method in ("GET", "POST")
        assert entry.credits >= 0
        assert entry.to_dict()["path"] == path

    for path in USED_BY_SCOUT:
        assert endpoint(path).credits >= 1, path
    assert endpoint(BALANCE_PATH).credits == 0  # the balance check is free and so never reserves
    assert endpoint("v1/reddit/search") is endpoint("/v1/reddit/search")  # the leading slash is optional


def test_endpoints_that_harvest_contact_details_or_infer_traits_are_not_on_the_allowlist() -> None:
    entries = load_endpoints()
    for path in (
        "/v1/find-creator-emails",
        "/v1/detect-age-gender",
        "/v1/linkme",
        "/v1/linktree",
        "/v1/linkbio",
        "/v1/pillar",
    ):
        assert path not in entries, path
    assert BALANCE_PATH == "/v1/credit-balance"  # the free alias; the spec's /v1/account/credit-balance shows a charge
