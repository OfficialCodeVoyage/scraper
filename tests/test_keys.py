"""Where the ScrapeCreators key comes from, what happens without one, and that it never leaks."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import types
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from support import DAY, FAKE_KEY, SC_HOST, FakeKeychain, FakeWeb

import scout.keys
from scout import api, claude_cli
from scout.cli import main
from scout.http import describe_error
from scout.keys import KEY_NAME, get_api_key, key_source
from scout.models import MissingKeyError, ProviderError
from scout.store import CreditLedger

# Lens with a Reddit subreddit (paid) and a Hacker News query (free), and no comment calls.
MIXED_PROFILE = """
niche: test niche
comments: {top_posts: 0, youtube_videos: 0, hn_stories: 0}
lenses:
  video_ideas:
    label: Ideas
    credit_budget: 5
    reddit:
      subreddits: [{name: one}]
    hn:
      queries: [bank run]
"""


# Where the key comes from


def test_the_environment_key_wins_and_the_keychain_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, keychain: FakeKeychain
) -> None:
    monkeypatch.setenv(KEY_NAME, "  env-key-not-real  ")
    keychain.find("keychain-key-not-real")

    assert get_api_key() == "env-key-not-real"
    assert key_source() == "environment"
    assert keychain.calls == []


def test_the_keychain_is_the_fallback_when_the_environment_has_no_key(
    monkeypatch: pytest.MonkeyPatch, keychain: FakeKeychain
) -> None:
    keychain.find("keychain-key-not-real")

    assert get_api_key() == "keychain-key-not-real"
    assert key_source() == "keychain"
    assert keychain.calls[-1][1:] == ["find-generic-password", "-s", KEY_NAME, "-w"]

    monkeypatch.setenv(KEY_NAME, "   ")  # a blank value is no key
    assert get_api_key() == "keychain-key-not-real"


def test_without_a_key_anywhere_there_is_none_and_off_macos_the_keychain_is_never_asked(
    monkeypatch: pytest.MonkeyPatch, keychain: FakeKeychain
) -> None:
    assert get_api_key() is None and key_source() is None  # the Keychain says "not found"
    keychain.find("")  # found, but empty
    assert get_api_key() is None
    for failure in (OSError("no security command"), subprocess.TimeoutExpired("security", 10)):
        keychain.error = failure
        assert get_api_key() is None

    keychain.error = None
    keychain.find("keychain-key-not-real")
    monkeypatch.setattr(scout.keys, "sys", types.SimpleNamespace(platform="linux"))
    asked_before = len(keychain.calls)
    assert get_api_key() is None
    assert len(keychain.calls) == asked_before


# Without a key


async def test_a_paid_call_without_a_key_raises_and_sends_nothing_while_the_free_sources_still_work(
    web: FakeWeb, load: Callable[[str], Any], ledger: CreditLedger
) -> None:
    web.on("hn.algolia.com", "/api/v1/search", body=load("free/hn_search"))

    with pytest.raises(MissingKeyError, match=KEY_NAME):
        await api.search("reddit", "bank run")
    with pytest.raises(MissingKeyError):
        await api.user_posts("youtube", "@examplechannel")
    with pytest.raises(MissingKeyError):
        await api.scrapecreators_call("/v1/reddit/search", {"query": "bank run"})
    free = await api.search("hn", "bank run")

    assert (free["count"], free["credits_used"]) == (2, 0)
    assert web.sent_to(SC_HOST) == []
    assert ledger.spent(DAY) == 0


async def test_a_profile_run_without_a_key_collects_the_free_sources_and_says_so(
    web: FakeWeb, load: Callable[[str], Any], add_profile: Callable[[str, str], Any]
) -> None:
    web.on("hn.algolia.com", "/api/v1/search", body=load("free/hn_search"))
    add_profile("test", MIXED_PROFILE)

    run = await api.collect("test")

    assert run["items"] == 2
    assert run["paid_calls"] == 0
    assert any("only the free sources ran" in w for w in run["warnings"])
    assert web.sent_to(SC_HOST) == []


# The key never leaks


async def test_the_key_goes_only_in_a_header_to_scrapecreators_and_redirects_are_not_followed(
    web: FakeWeb, with_key: str, load: Callable[[str], Any]
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body=load("scrapecreators/reddit_search"))
    web.on("hn.algolia.com", "/api/v1/search", body=load("free/hn_search"))
    await api.search("reddit", "bank run")
    await api.search("hn", "bank run")

    assert len(web.requests) == 2
    for request in web.requests:
        assert request.url.scheme == "https"
        assert FAKE_KEY not in str(request.url)
        assert (request.headers.get("x-api-key") == FAKE_KEY) == (request.url.host == SC_HOST)

    web.on(SC_HOST, "/v1/reddit/search", status=302, headers={"location": "https://other.example.test/collect"})
    with pytest.raises(ProviderError) as refused:
        await api.search("reddit", "bank run")
    assert refused.value.status == 302
    assert {r.url.host for r in web.requests} == {SC_HOST, "hn.algolia.com"}  # nobody else was contacted


async def test_the_key_is_masked_in_error_messages(web: FakeWeb, with_key: str) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy refused, x-api-key was {FAKE_KEY}")

    web.on(SC_HOST, "/v1/reddit/search", handler=refuse)
    with pytest.raises(ProviderError) as failed:
        await api.search("reddit", "bank run")
    assert FAKE_KEY not in str(failed.value)
    assert FAKE_KEY not in repr(failed.value)

    assert describe_error(ValueError(f"bad {FAKE_KEY} here"), [FAKE_KEY]) == "ValueError: bad *** here"
    request = httpx.Request("GET", f"https://api.example.test/?k={FAKE_KEY}")
    described = describe_error(httpx.ConnectError("no route", request=request), [FAKE_KEY])
    assert FAKE_KEY not in described and "***" in described


async def test_the_key_never_reaches_the_log(
    web: FakeWeb,
    with_key: str,
    load: Callable[[str], Any],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waited: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr("scout.http._sleep", no_sleep)
    answers = iter([429, 200])

    def busy_then_ok(request: httpx.Request) -> httpx.Response:
        if next(answers) == 429:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json=load("scrapecreators/reddit_search"))

    web.on(SC_HOST, "/v1/reddit/search", handler=busy_then_ok)
    caplog.set_level(logging.DEBUG)

    await api.search("reddit", "bank run")

    assert waited == [1.0]
    assert any("retrying" in record.getMessage() for record in caplog.records)  # something was logged to check
    assert FAKE_KEY not in caplog.text


def test_the_key_never_appears_in_cli_output(
    web: FakeWeb, with_key: str, load: Callable[[str], Any], capsys: pytest.CaptureFixture[str]
) -> None:
    web.on(SC_HOST, "/v1/reddit/search", body=load("scrapecreators/reddit_search"))
    shown: list[str] = []

    def run(argv: list[str], expected_exit: int) -> None:
        assert main(argv) == expected_exit, argv
        captured = capsys.readouterr()
        shown.append(captured.out + captured.err)

    run(["call", "/v1/reddit/search", "query=bank run", "--json"], 0)
    run(["search", "reddit", "bank run"], 0)
    run(["credits"], 0)
    run(["credits", "--json"], 0)

    web.on(SC_HOST, "/v1/reddit/search", status=500, body={"message": "upstream broke"})
    run(["search", "reddit", "bank run"], 2)

    def leak(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy refused, x-api-key was {FAKE_KEY}")

    web.on(SC_HOST, "/v1/reddit/search", handler=leak)
    run(["search", "reddit", "bank run"], 2)

    assert "".join(shown).strip()  # the commands did print
    assert FAKE_KEY not in "\n".join(shown)


async def test_the_summariser_child_gets_no_keys_and_runs_with_no_tools(
    monkeypatch: pytest.MonkeyPatch, with_key: str
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "other-key-not-real")
    started: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, data: bytes) -> tuple[bytes, bytes]:
            started["stdin"] = data
            answer = {"structured_output": {"ideas": []}, "modelUsage": {"fake-model": {}}, "duration_ms": 5}
            return json.dumps(answer).encode(), b""

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        started["args"] = list(args)
        started["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_cli, "claude_binary", lambda: "/fake/claude")

    answer = await claude_cli.ask_claude("the prompt", system="the system", schema={}, model="fake", timeout=5)

    assert answer.data == {"ideas": []}
    args = started["args"]
    assert "--safe-mode" in args and "--no-session-persistence" in args
    assert args[args.index("--tools") + 1] == ""  # every tool off
    assert started["stdin"] == b"the prompt"
    env = started["env"]
    assert not [name for name in env if "KEY" in name or "TOKEN" in name or name.startswith("ANTHROPIC")]
    assert FAKE_KEY not in env.values() and "other-key-not-real" not in env.values()


def test_the_summariser_child_keeps_the_claude_config_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/fake/claude-config")
    assert claude_cli.child_env()["CLAUDE_CONFIG_DIR"] == "/fake/claude-config"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert "CLAUDE_CONFIG_DIR" not in claude_cli.child_env()
