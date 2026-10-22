"""The local store: a post seen again is the same post, with its counts tracked over time."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from support import DAY, NOW

from scout.models import Comment, Item
from scout.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "scout.db")
    yield opened
    opened.close()


def test_the_same_post_seen_twice_is_stored_once_and_is_new_only_the_first_time(
    store: Store, make_item: Callable[..., Item]
) -> None:
    first = make_item("p1", lens="video_ideas", title="First title", score=100)
    again = make_item("p1", lens="dev_intel", title="Edited title", score=250)
    other_source = make_item("p1", source="hn", community="hn", url="https://news.ycombinator.com/item?id=1")
    later = NOW + timedelta(hours=8)

    assert store.save_items([first, first], NOW) == {"reddit:p1"}  # twice in one batch: still one new post
    assert store.save_items([again], later) == set()  # and not new on the next run
    assert store.first_seen(again) == NOW
    assert store.save_items([other_source], later) == {"hn:p1"}  # the same id on another source is another post

    stored = {item.key: item for item in store.items_of_day(DAY)}
    assert sorted(stored) == ["hn:p1", "reddit:p1"]
    assert stored["reddit:p1"].title == "Edited title"  # the latest text and the latest counts
    assert stored["reddit:p1"].score == 250
    # Both lenses found it today, so the brief shows it in both tabs.
    assert sorted(i.lens for i in store.items_of_day(DAY) if i.key == "reddit:p1") == ["dev_intel", "video_ideas"]


def test_every_sighting_leaves_a_snapshot_of_the_counts(store: Store, make_item: Callable[..., Item]) -> None:
    store.save_items([make_item("p1", score=100, comments=10)], NOW)
    later = NOW + timedelta(hours=8)
    store.save_items([make_item("p1", score=250, comments=40)], later)
    post = make_item("p1")

    assert store.previous_snapshot(post, NOW) is None  # nothing was taken before the first sighting
    assert store.previous_snapshot(post, later) == (NOW, 100, 10)
    assert store.previous_snapshot(post, later + timedelta(hours=1)) == (later, 250, 40)


def test_a_comment_fetched_again_is_stored_once_with_its_latest_score(
    store: Store, make_item: Callable[..., Item]
) -> None:
    def comment(ext_id: str, score: int) -> Comment:
        return Comment("reddit", "p1", ext_id, "fake_commenter", f"text of {ext_id}", score, NOW)

    store.save_comments([comment("c1", 3), comment("c2", 9)])
    store.save_comments([comment("c1", 30)])

    stored = store.comments_for(make_item("p1"))
    assert [(c.ext_id, c.score) for c in stored] == [("c1", 30), ("c2", 9)]  # best first, no duplicate


def test_a_post_found_later_by_the_other_lens_is_filed_under_that_lens_on_that_day(
    store: Store, make_item: Callable[..., Item]
) -> None:
    store.save_items([make_item("p1", lens="video_ideas")], NOW)
    next_day = NOW + timedelta(days=1)
    store.save_items([make_item("p1", lens="dev_intel", watch="hn: tools")], next_day)

    assert [(i.lens, i.watch) for i in store.items_of_day(DAY)] == [("video_ideas", "r/ExampleSub")]
    assert [(i.lens, i.watch) for i in store.items_of_day(next_day.date().isoformat())] == [("dev_intel", "hn: tools")]
