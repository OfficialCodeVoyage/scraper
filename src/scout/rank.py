"""Scoring the day's items before Claude sees them: duplicates out, floors, and a signal per item.

The signals follow last30days-skill (github.com/mvanhorn/last30days-skill, MIT; ideas reapplied, no code copied):
engagement over ``sqrt(age in days + 1)``, 1.5x under a week; a bonus of 0.15 per extra source that carries the same
link; an engagement floor so a quiet post cannot win. Added here: Reddit's debate signal from the upvote
ratio, a subreddit percentile against our own 30 days of history, the YouTube outlier multiple (views over the
channel's median) and question demand in the comments. Everything here is pure and costs nothing.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scout.models import Comment, Item
from scout.store import Store

FLOORS = {"reddit": 25, "hn": 25, "x": 25, "bluesky": 10, "threads": 10}
YOUTUBE_MIN_VIEWS = 20_000
# A multiple of the channel's median counts only above this many views: on a channel with a 1,300-view median a
# 357x "outlier" is noise.
YOUTUBE_OUTLIER_MIN_VIEWS = 50_000
YOUTUBE_MIN_OUTLIER = 2.0
FRESH_HOURS = 36
# Older than this is not news, however new to us: a channel's 30 latest videos reach back months.
MAX_AGE_DAYS = 30
_TRACKING = re.compile(r"^(utm_|ref$|ref_|fbclid$|gclid$|s$|t$|si$|feature$)")
_WORD = re.compile(r"[a-z0-9']+")
_QUESTION_START = re.compile(
    r"^(how|what|why|which|who|where|when|is|are|can|could|does|do|did|should|would|will|anyone|any)\b",
    re.IGNORECASE,
)


@dataclass
class Ranked:
    item: Item
    signal: float
    signals: dict[str, object] = field(default_factory=dict)
    also: list[Item] = field(default_factory=list)

    @property
    def sources(self) -> set[str]:
        return {self.item.source, *(i.source for i in self.also)}


def engagement(item: Item) -> int:
    """The item's interactions on its own scale (views for YouTube, stars for GitHub)."""
    reposts = item.extra.get("reposts")
    return item.score + item.comments + (reposts if isinstance(reposts, int) else 0)


def age_days(item: Item, now: datetime) -> float:
    if item.created_at is None:
        return 7.0
    return max(0.0, (now - item.created_at).total_seconds() / 86400)


def velocity(item: Item, now: datetime) -> float:
    age = age_days(item, now)
    value = engagement(item) / math.sqrt(age + 1)
    return value * 1.5 if age < 7 else value


def debate(item: Item) -> float | None:
    """How split the votes are, weighted by how much talk there is (Reddit only: it needs the upvote ratio)."""
    if item.upvote_ratio is None:
        return None
    return round((1 - abs(2 * float(item.upvote_ratio) - 1)) * math.log1p(item.comments), 2)


def normalise_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if not host:
        return None
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    return urlunsplit(("https", host, parts.path.rstrip("/"), query, ""))


def simhash(text: str) -> int:
    """A 64-bit SimHash of the word 3-shingles of ``text``: near-identical texts differ in few bits."""
    words = _WORD.findall(text.lower())
    shingles = [" ".join(words[i : i + 3]) for i in range(max(1, len(words) - 2))] or [""]
    weights = [0] * 64
    for shingle in shingles:
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if digest >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if weights[bit] > 0)


def _distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def percentile(values: list[int], value: float) -> float:
    """Share of ``values`` at or below ``value``, 0-100."""
    if not values:
        return 0.0
    return round(100 * sum(1 for v in values if v <= value) / len(values), 1)


def question_demand(comments: Iterable[Comment]) -> dict[str, int]:
    """Questions in the comments: how many, their likes, and the "gold nuggets" (a question with 5+ likes)."""
    asked = [
        c for c in comments if "?" in c.text and (_QUESTION_START.match(c.text.strip()) or c.text.strip().endswith("?"))
    ]
    return {
        "questions": len(asked),
        "question_likes": sum(c.score for c in asked),
        "gold": sum(1 for c in asked if c.score >= 5),
    }


def outlier(item: Item) -> float | None:
    """The YouTube video's views over its channel's median, when the video has the views to make that mean something."""
    value = item.extra.get("outlier")
    if not isinstance(value, int | float) or item.score < YOUTUBE_OUTLIER_MIN_VIEWS:
        return None
    return float(value)


def passes_floor(item: Item) -> bool:
    if item.source == "youtube":
        return item.score >= YOUTUBE_MIN_VIEWS
    if item.source == "github":
        return True
    return engagement(item) >= FLOORS.get(item.source, 0)


def _merge_duplicates(items: list[Item], now: datetime) -> list[Ranked]:
    """One entry per story: the same link on several sources, or a near-identical text, is kept once."""
    ordered = sorted(items, key=lambda i: velocity(i, now), reverse=True)
    kept: list[Ranked] = []
    by_link: dict[str, Ranked] = {}
    hashes: list[tuple[int, Ranked]] = []
    for item in ordered:
        link = normalise_url(item.link)
        if link and link in by_link:
            by_link[link].also.append(item)
            continue
        text = f"{item.title} {item.text}"
        digest = simhash(text) if len(text.split()) >= 8 else None
        twin = next(
            (r for h, r in hashes if digest is not None and _distance(h, digest) <= 3),
            None,
        )
        if twin is not None:
            twin.also.append(item)
            continue
        ranked = Ranked(item=item, signal=0.0)
        kept.append(ranked)
        if link:
            by_link[link] = ranked
        if digest is not None:
            hashes.append((digest, ranked))
    return kept


def rank(items: list[Item], store: Store, now: datetime, *, fresh_only: bool = True) -> list[Ranked]:
    """The day's items for one lens, best first, each with the signals shown in the brief.

    With ``fresh_only`` an item is kept when it was first seen after the previous day's brief (in the last 36 hours
    before there was one), or it is rising (its engagement grew by half and by 50 since an earlier snapshot): the
    brief is about what is new.
    """
    # A daily run 24 hours after the last one would still find that run's items inside 36 hours and show them again.
    fresh_after = store.previous_run(now.date().isoformat()) or (now - timedelta(hours=FRESH_HOURS))
    candidates: list[Item] = []
    rising: dict[str, float] = {}
    for item in items:
        if not passes_floor(item) or age_days(item, now) > MAX_AGE_DAYS:
            continue
        before = store.previous_snapshot(item, now - timedelta(hours=6))
        if before is not None:
            _, score, comments = before
            grown = engagement(item) - (score + comments)
            if grown >= 50 and grown >= 0.5 * max(1, score + comments):
                rising[item.key] = grown
        seen = store.first_seen(item)
        is_fresh = seen is None or seen > fresh_after
        if fresh_only and not is_fresh and item.key not in rising:
            continue
        candidates.append(item)

    groups = _merge_duplicates(candidates, now)
    by_source: dict[str, list[float]] = {}
    for group in groups:
        by_source.setdefault(group.item.source, []).append(velocity(group.item, now))

    month_ago = now - timedelta(days=30)
    for group in groups:
        item = group.item
        vel = velocity(item, now)
        peers = by_source[item.source]
        source_pct = sum(1 for v in peers if v <= vel) / len(peers)
        signals: dict[str, object] = {
            "engagement": engagement(item),
            "velocity": round(vel, 1),
            "age_days": round(age_days(item, now), 1),
        }
        bonus = 1 + 0.15 * (len(group.sources) - 1)
        if len(group.sources) > 1:
            signals["sources"] = sorted(group.sources)
        if item.key in rising:
            signals["rising"] = int(rising[item.key])
            bonus *= 1.1
        split = debate(item)
        if split is not None:
            signals["debate"] = split
            bonus *= 1 + min(split, 6) / 30
        if item.source == "reddit":
            history = store.community_scores("reddit", item.community, month_ago)
            if len(history) >= 20:
                signals["subreddit_pct"] = percentile(history, item.score)
        multiple = outlier(item)
        if multiple is not None:
            signals["outlier"] = multiple
            # Log scale: 2x gives 1.17, 8x 1.5, 64x and above 2.
            bonus *= 1 + min(math.log2(max(multiple, 1.0)), 6) / 6
        group.signal = round(source_pct * bonus, 4)
        group.signals = signals
    return sorted(groups, key=lambda g: g.signal, reverse=True)
