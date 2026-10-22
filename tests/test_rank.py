"""Ranking: what is new since the previous brief, one entry per story, and the floors."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from support import NOW

import scout.store
from scout.models import Item
from scout.rank import Ranked, rank
from scout.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "scout.db")
    yield opened
    opened.close()


@pytest.fixture
def brief_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """``brief_clock(when)``: the time ``Store.save_run`` stamps a finished brief with."""
    stamp = {"now": NOW}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return stamp["now"] if tz is None else stamp["now"].astimezone(tz)

    monkeypatch.setattr(scout.store, "datetime", Frozen)

    def set_time(when: datetime) -> None:
        stamp["now"] = when

    return set_time


def ids(ranked: Sequence[Ranked]) -> list[str]:
    return [r.item.ext_id for r in ranked]


def test_the_brief_shows_only_what_was_first_seen_after_the_previous_brief(
    store: Store, brief_clock: Callable[[datetime], None], make_item: Callable[..., Item]
) -> None:
    last_brief = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    old = make_item("old", created_at=NOW - timedelta(days=2))
    new = make_item("new")
    store.save_items([old], last_brief - timedelta(hours=20))
    assert store.previous_run("2026-10-19") is None  # before the first brief there is nothing to compare with
    brief_clock(last_brief)
    store.save_run("2026-10-19", Path("brief.html"), {})
    store.save_items([new], NOW - timedelta(hours=2))

    assert store.previous_run("2026-10-20") == last_brief
    assert ids(rank([old, new], store, NOW)) == ["new"]


def test_an_older_post_comes_back_when_it_grew_by_half_and_by_fifty(
    store: Store, brief_clock: Callable[[datetime], None], make_item: Callable[..., Item]
) -> None:
    seen = NOW - timedelta(hours=20)
    store.save_items([make_item("surging", score=100, comments=10), make_item("flat", score=100, comments=10)], seen)
    brief_clock(NOW - timedelta(hours=12))
    store.save_run("2026-10-19", Path("brief.html"), {})

    surging = make_item("surging", score=200, comments=40)  # 110 -> 240
    flat = make_item("flat", score=120, comments=10)  # 110 -> 130
    ranked = rank([surging, flat], store, NOW)

    assert ids(ranked) == ["surging"]
    assert ranked[0].signals["rising"] == 130


def test_one_story_carried_by_several_sources_is_ranked_once(store: Store, make_item: Callable[..., Item]) -> None:
    reddit = make_item("r1", link="https://www.example.com/story?utm_source=feed&id=7", score=300)
    hn = make_item(
        "h1",
        source="hn",
        community="hn",
        url="https://news.ycombinator.com/item?id=1",
        link="http://example.com/story/?id=7&utm_campaign=x",
        score=200,
        comments=50,
    )
    different = make_item("o1", link="https://example.com/a-different-story", score=150)

    ranked = rank([reddit, hn, different], store, NOW)

    assert len(ranked) == 2
    story = next(r for r in ranked if r.item.ext_id in ("r1", "h1"))
    assert story.item.ext_id == "r1"
    assert story.sources == {"reddit", "hn"}
    assert story.signals["sources"] == ["hn", "reddit"]


def test_quiet_posts_old_posts_and_low_view_videos_are_left_out(store: Store, make_item: Callable[..., Item]) -> None:
    keep = make_item("keep", score=100)
    quiet = make_item("quiet", score=3, comments=2)  # under the Reddit floor
    ancient = make_item("ancient", created_at=NOW - timedelta(days=45))
    unwatched = make_item("unwatched", source="youtube", score=5_000, comments=1)  # under the YouTube floor

    assert ids(rank([keep, quiet, ancient, unwatched], store, NOW)) == ["keep"]
