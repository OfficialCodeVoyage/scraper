"""The command line: the smoke path of each kind of command, with every request answered by the fake web."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support import REPO, FakeWeb

from scout.cli import main

EXAMPLE_PROFILE = REPO / "examples" / "profiles" / "faceless-youtube.yaml"
Capsys = pytest.CaptureFixture[str]


def test_scout_endpoints_lists_the_allowlist_as_text_and_as_json(capsys: Capsys) -> None:
    assert main(["endpoints"]) == 0
    text = capsys.readouterr().out
    assert "GET /v1/reddit/search (1 credit(s)) [query," in text

    assert main(["endpoints", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == len(data["endpoints"]) > 0
    assert "/v1/reddit/search" in [e["path"] for e in data["endpoints"]]
    assert data["credits_used"] == 0


def test_scout_profiles_says_so_when_there_are_none(scout_home: Path, capsys: Capsys) -> None:
    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert f"Profiles in {scout_home}" in out
    assert "none yet" in out


def test_scout_profiles_lists_a_valid_profile_and_the_error_of_a_broken_one(
    scout_home: Path, add_profile: Callable[[str, str], Path], capsys: Capsys
) -> None:
    add_profile("default", EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    add_profile("broken", "niche: x\nlenses:\n  video_ideas: {label: L, credit_budget: 1, bogus: 1}\n")

    assert main(["profiles", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    rows = {row["name"]: row for row in data["profiles"]}
    assert rows["default"]["valid"] is True
    assert rows["default"]["lenses"] == ["video_ideas", "dev_intel"]
    assert rows["default"]["credit_budget"] == 60
    assert rows["broken"]["valid"] is False and rows["broken"]["error"]
    assert data["default_exists"] is True
    assert data["config_dir"] == str(scout_home)

    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "default: video_ideas, dev_intel (lens budgets 60 credits)" in out
    assert "broken: does not load" in out


def test_scout_search_hn_prints_json_and_readable_lines_and_costs_nothing(
    web: FakeWeb, load: Callable[[str], Any], capsys: Capsys
) -> None:
    web.on("hn.algolia.com", "/api/v1/search", body=load("free/hn_search"))

    assert main(["search", "hn", "bank run", "--json", "--limit", "5", "--since-days", "7"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["source"], data["count"], data["credits_used"]) == ("hn", 2, 0)
    assert [item["id"] for item in data["items"]] == ["9000001", "9000002"]
    assert web.requests[0].url.params["hitsPerPage"] == "5"

    assert main(["search", "hn", "bank run"]) == 0
    out = capsys.readouterr().out
    assert "[hn] 321 · 88 comments · 2026-09-21  Example Corp files for bankruptcy" in out
    assert "2 items; credits used 0" in out
    assert {r.url.host for r in web.requests} == {"hn.algolia.com"}


def test_scout_search_dry_run_shows_the_cost_and_calls_nothing(web: FakeWeb, capsys: Capsys) -> None:
    assert main(["search", "reddit", "bank run", "--since-days", "7", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Dry run, nothing was called" in out
    assert "/v1/reddit/search" in out and "1 credit(s)" in out
    assert "Credits needed: 1; 100 of 100 left today" in out
    assert "no ScrapeCreators key found" in out
    assert web.requests == []


def test_scout_refuses_with_exit_1_and_one_error_line_on_stderr(web: FakeWeb, capsys: Capsys) -> None:
    refusals = [
        (["call", "/v1/not/on/the/list"], "allowlist"),
        (["call", "/v1/reddit/search"], "needs query"),
        (["search", "reddit", "bank run"], "SCRAPECREATORS_API_KEY"),  # no key in this test
        (["credits"], "SCRAPECREATORS_API_KEY"),
        (["search", "hn", "   "], "query must not be empty"),
        (["search", "hn", "bank run", "--since-days", "0"], "since_days"),
    ]
    for argv, message in refusals:
        assert main(argv) == 1, argv
        out, err = capsys.readouterr()
        assert out == "", argv
        assert err.startswith("error: ") and message in err, argv
        assert len(err.strip().splitlines()) == 1, argv

    assert web.requests == []


def test_a_call_the_cap_refuses_exits_1_in_text_and_in_json(
    web: FakeWeb, with_key: str, set_cap: Callable[[int], None], capsys: Capsys
) -> None:
    set_cap(0)
    assert main(["search", "reddit", "bank run"]) == 1
    assert "Skipped: Credit cap" in capsys.readouterr().out
    assert main(["search", "reddit", "bank run", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["skipped"] is True
    assert main(["call", "/v1/reddit/search", "query=bank run"]) == 1
    capsys.readouterr()
    assert web.paid == []


def test_scout_credits_without_a_key_exits_1_in_json_too(web: FakeWeb, capsys: Capsys) -> None:
    assert main(["credits", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["key_found"] is False
    assert web.requests == []
