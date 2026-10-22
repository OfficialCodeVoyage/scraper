"""The global config and the profiles are validated when they load, before any credit is spent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import REPO

from scout import api
from scout.config import load_global_config, load_profile

EXAMPLE_PROFILE = REPO / "examples" / "profiles" / "faceless-youtube.yaml"

BUDGETS_85 = """
niche: test niche
comments: {top_posts: 20, youtube_videos: 5, hn_stories: 10}
lenses:
  video_ideas: {label: Ideas, credit_budget: 25}
  dev_intel: {label: Dev, credit_budget: 35}
"""


def test_the_daily_cap_defaults_to_100_and_a_misspelled_key_is_an_error(
    set_cap: Callable[[int], None], scout_home: Path
) -> None:
    assert load_global_config().daily_credit_cap == 100  # no config file

    set_cap(40)
    assert load_global_config().daily_credit_cap == 40

    (scout_home / "config.yaml").write_text("daily_credits_cap: 40\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_global_config()
    (scout_home / "config.yaml").write_text("daily_credit_cap: -1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_global_config()


def test_lens_budgets_that_do_not_fit_the_daily_cap_are_refused_when_the_profile_loads(
    add_profile: Callable[[str, str], Path],
) -> None:
    add_profile("test", BUDGETS_85)  # 25 + 35 for the lenses and 20 + 5 for comment calls: 85

    with pytest.raises(ValueError, match="daily_credit_cap"):
        load_profile("test", cap=80)
    assert load_profile("test", cap=85).name == "test"


def test_a_misspelled_key_or_an_unknown_lens_is_refused_when_the_profile_loads(
    add_profile: Callable[[str, str], Path],
) -> None:
    add_profile("typo", "niche: x\nlenses:\n  video_ideas: {label: L, credit_budget: 1, redit: {}}\n")
    add_profile("unknown-lens", "niche: x\nlenses:\n  podcast_ideas: {label: L, credit_budget: 1}\n")
    add_profile("no-lens", "niche: x\nlenses: {}\n")

    for name in ("typo", "unknown-lens", "no-lens"):
        with pytest.raises(ValueError):
            load_profile(name, cap=100)


def test_the_example_profile_loads_and_costs_what_its_header_says(
    add_profile: Callable[[str, str], Path],
) -> None:
    add_profile("default", EXAMPLE_PROFILE.read_text(encoding="utf-8"))

    profile = load_profile("default", cap=100)
    estimate = api.estimate(profile="default")

    assert list(profile.lenses) == ["video_ideas", "dev_intel"]
    assert estimate["credits"] == 53  # "Expected here: ... so at most 53 credits a day." in the file's header
    assert estimate["credits"] <= estimate["daily_credit_cap"]
    assert estimate["credits_used"] == 0
