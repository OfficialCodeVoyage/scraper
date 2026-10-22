"""scout's history in SQLite: one credit ledger for everything, and one store per profile.

``credits.db`` (``CreditLedger``) counts every ScrapeCreators call of every profile and every ad-hoc call, so the daily
cap is global. Before a call its credits are reserved inside an immediate transaction that refuses to pass the day's
cap; after the answer the row is set to what the answer says it cost (``credits_charged``, 0 for a cached answer), so
the day's total is what ScrapeCreators charged. A call that failed without an answer keeps its reservation.

``profiles/<name>/scout.db`` (``Store``) holds a profile's posts, a snapshot of their counts per run, which lens found
each post on which day (a post both lenses find is in both tabs), comments, cached summaries (the same prompt and input
are never asked twice), tool mentions and runs. Post text is untrusted and stays in this file and the brief.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from scout.models import Comment, CreditCapReached, Item
from scout.paths import credits_db_path, profile_data_dir

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, ts TEXT NOT NULL, endpoint TEXT NOT NULL,
    credits INTEGER NOT NULL, remaining INTEGER, kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS credit_ledger_day ON credit_ledger (day);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    source TEXT NOT NULL, ext_id TEXT NOT NULL, lens TEXT NOT NULL, watch TEXT NOT NULL, url TEXT NOT NULL,
    title TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL, community TEXT NOT NULL, created_at TEXT,
    link TEXT, extra TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (source, ext_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    source TEXT NOT NULL, ext_id TEXT NOT NULL, day TEXT NOT NULL, taken_at TEXT NOT NULL,
    score INTEGER NOT NULL, comments INTEGER NOT NULL, upvote_ratio REAL, views INTEGER,
    PRIMARY KEY (source, ext_id, taken_at)
);
CREATE INDEX IF NOT EXISTS snapshots_day ON snapshots (day);
CREATE TABLE IF NOT EXISTS sightings (
    source TEXT NOT NULL, ext_id TEXT NOT NULL, lens TEXT NOT NULL, day TEXT NOT NULL, watch TEXT NOT NULL,
    PRIMARY KEY (source, ext_id, lens, day)
);
CREATE INDEX IF NOT EXISTS sightings_day ON sightings (day);
CREATE TABLE IF NOT EXISTS comments (
    source TEXT NOT NULL, post_id TEXT NOT NULL, ext_id TEXT NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL,
    score INTEGER NOT NULL, created_at TEXT,
    PRIMARY KEY (source, ext_id)
);
CREATE INDEX IF NOT EXISTS comments_post ON comments (source, post_id);
CREATE TABLE IF NOT EXISTS summaries (
    lens TEXT NOT NULL, input_hash TEXT NOT NULL, day TEXT NOT NULL, prompt_hash TEXT NOT NULL, json TEXT NOT NULL,
    PRIMARY KEY (lens, input_hash)
);
CREATE TABLE IF NOT EXISTS tool_mentions (
    tool TEXT NOT NULL, source TEXT NOT NULL, ext_id TEXT NOT NULL, author TEXT NOT NULL, day TEXT NOT NULL,
    PRIMARY KEY (tool, source, ext_id)
);
CREATE TABLE IF NOT EXISTS runs (
    day TEXT PRIMARY KEY, finished_at TEXT NOT NULL, brief_path TEXT NOT NULL, notes TEXT NOT NULL
);
"""


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(schema)
    return db


class CreditLedger:
    """The ScrapeCreators credit ledger shared by every profile and ad-hoc call; closed with ``close``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db = _connect(path, _LEDGER_SCHEMA)

    @classmethod
    def default(cls) -> CreditLedger:
        return cls(credits_db_path())

    def close(self) -> None:
        self._db.close()

    def reserve_credit(self, endpoint: str, cap: int, day: str, credits: int = 1) -> int:
        """Reserve ``credits`` for a call to ``endpoint``; return the ledger row id.

        Raises:
            CreditCapReached: the day's calls plus this one would pass ``cap``; nothing was reserved.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            spent = self._spent(day)
            if spent + credits > cap:
                raise CreditCapReached(
                    f"{spent} of {cap} ScrapeCreators credits already used on {day}"
                    + (f" and this call needs {credits}" if credits != 1 else "")
                )
            cursor = self._db.execute(
                "INSERT INTO credit_ledger (day, ts, endpoint, credits, remaining, kind) VALUES (?,?,?,?,NULL,'call')",
                (day, _iso(datetime.now(UTC)), endpoint, credits),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return int(cursor.lastrowid or 0)

    def settle_credit(self, row_id: int, credits: int, remaining: int | None) -> None:
        """Set a reserved row to what the call cost: less refunds the difference, more records the extra."""
        with self._db:
            self._db.execute(
                "UPDATE credit_ledger SET credits=?, remaining=? WHERE id=?",
                (credits, remaining, row_id),
            )

    def _spent(self, day: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(credits), 0) AS n FROM credit_ledger WHERE day=? AND kind='call'",
            (day,),
        ).fetchone()
        return int(row["n"])

    def spent(self, day: str) -> int:
        """Credits scout's own calls used on ``day`` (what the cap counts)."""
        return self._spent(day)

    def left(self, day: str, cap: int) -> int:
        """Credits the cap still allows on ``day``."""
        return max(0, cap - self._spent(day))

    def record_balance(self, balance: int, day: str) -> int:
        """Note the account's balance; return the credits used elsewhere (MCP, other tools) since the last note.

        The last known balance (a note, or an answer's ``credits_remaining``) less what scout's own calls cost since,
        less the balance now, is what was used elsewhere; it is recorded as ``external`` and shown, and the cap counts
        only scout's calls.
        """
        last = self._db.execute(
            "SELECT id, remaining FROM credit_ledger WHERE remaining IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        external = 0
        if last:
            ours = self._db.execute(
                "SELECT COALESCE(SUM(credits), 0) AS n FROM credit_ledger WHERE kind='call' AND id > ?",
                (last["id"],),
            ).fetchone()
            external = max(0, int(last["remaining"]) - int(ours["n"]) - balance)
        with self._db:
            self._db.execute(
                "INSERT INTO credit_ledger (day, ts, endpoint, credits, remaining, kind) VALUES (?,?,?,?,?,?)",
                (
                    day,
                    _iso(datetime.now(UTC)),
                    "credit-balance",
                    external,
                    balance,
                    "external" if external else "balance",
                ),
            )
        return external

    def external(self, day: str) -> int:
        row = self._db.execute(
            "SELECT COALESCE(SUM(credits), 0) AS n FROM credit_ledger WHERE day=? AND kind='external'",
            (day,),
        ).fetchone()
        return int(row["n"])

    def last_balance(self) -> int | None:
        row = self._db.execute(
            "SELECT remaining FROM credit_ledger WHERE remaining IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["remaining"]) if row else None

    def endpoint_use(self, day: str) -> list[tuple[str, int, int]]:
        """(endpoint, calls, credits) of scout's calls on ``day``, most credits first."""
        rows = self._db.execute(
            "SELECT endpoint, COUNT(*) AS calls, SUM(credits) AS credits FROM credit_ledger "
            "WHERE day=? AND kind='call' GROUP BY endpoint ORDER BY credits DESC",
            (day,),
        ).fetchall()
        return [(row["endpoint"], row["calls"], row["credits"]) for row in rows]


class Store:
    """A profile's SQLite history; one connection, closed with ``close``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db = _connect(path, _SCHEMA)

    @classmethod
    def for_profile(cls, name: str) -> Store:
        return cls(profile_data_dir(name) / "scout.db")

    def close(self) -> None:
        self._db.close()

    # Items and their snapshots

    def save_items(self, items: Iterable[Item], now: datetime) -> set[str]:
        """Store each item, a snapshot of its counts and the lens that found it today; return the keys seen for the
        first time. The post keeps the lens that found it first; ``sightings`` holds every lens that found it."""
        new: set[str] = set()
        day = now.date().isoformat()
        with self._db:
            for item in items:
                inserted = self._db.execute(
                    "INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.source,
                        item.ext_id,
                        item.lens,
                        item.watch,
                        item.url,
                        item.title,
                        item.text,
                        item.author,
                        item.community,
                        _iso(item.created_at),
                        item.link,
                        json.dumps(item.extra, default=str),
                        _iso(now),
                    ),
                ).rowcount
                if inserted:
                    new.add(item.key)
                else:
                    self._db.execute(
                        "UPDATE posts SET title=?, text=?, extra=? WHERE source=? AND ext_id=?",
                        (
                            item.title,
                            item.text,
                            json.dumps(item.extra, default=str),
                            item.source,
                            item.ext_id,
                        ),
                    )
                if item.lens:
                    self._db.execute(
                        "INSERT OR REPLACE INTO sightings VALUES (?,?,?,?,?)",
                        (item.source, item.ext_id, item.lens, day, item.watch),
                    )
                self._db.execute(
                    "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
                    (
                        item.source,
                        item.ext_id,
                        day,
                        _iso(now),
                        item.score,
                        item.comments,
                        item.upvote_ratio,
                        item.views,
                    ),
                )
        return new

    def first_seen(self, item: Item) -> datetime | None:
        row = self._db.execute(
            "SELECT first_seen FROM posts WHERE source=? AND ext_id=?",
            (item.source, item.ext_id),
        ).fetchone()
        return _parse(row["first_seen"]) if row else None

    def previous_snapshot(self, item: Item, before: datetime) -> tuple[datetime, int, int] | None:
        """The latest snapshot of ``item`` taken before ``before``: (taken_at, score, comments)."""
        row = self._db.execute(
            "SELECT taken_at, score, comments FROM snapshots WHERE source=? AND ext_id=? AND taken_at < ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (item.source, item.ext_id, _iso(before)),
        ).fetchone()
        if not row:
            return None
        taken = _parse(row["taken_at"])
        return (taken, row["score"], row["comments"]) if taken else None

    def community_scores(self, source: str, community: str, since: datetime) -> list[int]:
        """The latest score of every post of ``community`` snapshotted since ``since`` (for a percentile)."""
        rows = self._db.execute(
            "SELECT MAX(s.score) AS score FROM snapshots s JOIN posts p USING (source, ext_id) "
            "WHERE p.source=? AND p.community=? AND s.taken_at >= ? GROUP BY s.source, s.ext_id",
            (source, community, _iso(since)),
        ).fetchall()
        return [row["score"] for row in rows]

    def items_of_day(self, day: str) -> list[Item]:
        """Every item snapshotted on ``day``, with that day's latest counts (to render a brief again), once per lens
        that found it that day (a store from before lenses were tracked per day gives the post's first lens)."""
        rows = self._db.execute(
            "SELECT p.*, s.score, s.comments, s.upvote_ratio, s.views FROM posts p JOIN snapshots s "
            "USING (source, ext_id) WHERE s.day=? AND s.taken_at = (SELECT MAX(taken_at) FROM snapshots "
            "WHERE source=p.source AND ext_id=p.ext_id AND day=?)",
            (day, day),
        ).fetchall()
        seen: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in self._db.execute("SELECT source, ext_id, lens, watch FROM sightings WHERE day=?", (day,)):
            seen.setdefault((row["source"], row["ext_id"]), []).append((row["lens"], row["watch"]))
        items: list[Item] = []
        for row in rows:
            for lens, watch in seen.get((row["source"], row["ext_id"])) or [(row["lens"], row["watch"])]:
                items.append(
                    Item(
                        source=row["source"],
                        ext_id=row["ext_id"],
                        url=row["url"],
                        title=row["title"],
                        text=row["text"],
                        author=row["author"],
                        community=row["community"],
                        created_at=_parse(row["created_at"]),
                        score=row["score"],
                        comments=row["comments"],
                        lens=lens,
                        watch=watch,
                        upvote_ratio=row["upvote_ratio"],
                        views=row["views"],
                        link=row["link"],
                        extra=json.loads(row["extra"]),
                    )
                )
        return items

    # Comments

    def save_comments(self, comments: Iterable[Comment]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO comments VALUES (?,?,?,?,?,?,?)",
                [(c.source, c.post_id, c.ext_id, c.author, c.text, c.score, _iso(c.created_at)) for c in comments],
            )

    def comments_for(self, item: Item) -> list[Comment]:
        rows = self._db.execute(
            "SELECT * FROM comments WHERE source=? AND post_id=? ORDER BY score DESC",
            (item.source, item.ext_id),
        ).fetchall()
        return [
            Comment(
                source=row["source"],
                post_id=row["post_id"],
                ext_id=row["ext_id"],
                author=row["author"],
                text=row["text"],
                score=row["score"],
                created_at=_parse(row["created_at"]),
            )
            for row in rows
        ]

    # Summaries, tool mentions, runs

    def cached_summary(self, lens: str, input_hash: str) -> dict | None:
        row = self._db.execute(
            "SELECT json FROM summaries WHERE lens=? AND input_hash=?",
            (lens, input_hash),
        ).fetchone()
        return json.loads(row["json"]) if row else None

    def save_summary(self, lens: str, input_hash: str, day: str, prompt_hash: str, data: dict) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO summaries VALUES (?,?,?,?,?)",
                (lens, input_hash, day, prompt_hash, json.dumps(data)),
            )

    def save_tool_mentions(self, day: str, mentions: Iterable[tuple[str, str, str, str]]) -> None:
        """Record (tool, source, ext_id, author) mentions; a repeat of the same post is kept once."""
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO tool_mentions VALUES (?,?,?,?,?)",
                [(tool, source, ext_id, author, day) for tool, source, ext_id, author in mentions],
            )

    def tool_authors(self, tool: str, since_day: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(DISTINCT author) AS n FROM tool_mentions WHERE tool=? AND day >= ?",
            (tool, since_day),
        ).fetchone()
        return int(row["n"])

    def save_run(self, day: str, brief_path: Path, notes: dict) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                (day, _iso(datetime.now(UTC)), str(brief_path), json.dumps(notes, default=str)),
            )

    def previous_run(self, before_day: str) -> datetime | None:
        """When the last brief of a day before ``before_day`` was finished; None before the first one."""
        row = self._db.execute(
            "SELECT finished_at FROM runs WHERE day < ? ORDER BY day DESC LIMIT 1",
            (before_day,),
        ).fetchone()
        return _parse(row["finished_at"]) if row else None

    def run_notes(self, day: str) -> dict | None:
        row = self._db.execute("SELECT notes FROM runs WHERE day=?", (day,)).fetchone()
        return json.loads(row["notes"]) if row else None
