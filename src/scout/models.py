"""What the sources return, normalised, and the errors of the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Item:
    """One post, video or repository from any source, in the shape the ranking, the brief and the API read.

    ``score`` is the source's main engagement count: Reddit score, likes on X, Threads and Bluesky, Hacker News
    points, GitHub stars, YouTube views. ``comments`` is its reply or comment count. ``key`` is unique across sources.
    ``lens`` and ``watch`` say which lens of a profile and which watch found it (empty for an ad-hoc call).
    """

    source: str
    ext_id: str
    url: str
    title: str
    text: str
    author: str
    community: str
    created_at: datetime | None
    score: int
    comments: int
    lens: str
    watch: str
    upvote_ratio: float | None = None
    views: int | None = None
    link: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ext_id}"

    def to_dict(self) -> dict[str, Any]:
        """The item as JSON-serialisable data (the shape every API function returns items in)."""
        return {
            "source": self.source,
            "id": self.ext_id,
            "key": self.key,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "author": self.author,
            "community": self.community,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "score": self.score,
            "comments": self.comments,
            "views": self.views,
            "upvote_ratio": self.upvote_ratio,
            "link": self.link,
            "extra": {k: _plain(v) for k, v in self.extra.items()},
        }


@dataclass
class Comment:
    """One comment or reply under an item."""

    source: str
    post_id: str
    ext_id: str
    author: str
    text: str
    score: int
    created_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "post_id": self.post_id,
            "id": self.ext_id,
            "author": self.author,
            "text": self.text,
            "score": self.score,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _plain(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


class ScoutError(Exception):
    """Anything scout refuses or cannot do."""


class ProviderError(ScoutError):
    """A source answered with an error or with something that is not what it documents.

    ``status`` is the HTTP status when the source answered with one.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CreditCapReached(ScoutError):
    """The day's ScrapeCreators credit cap would be passed by one more call; no call was made."""


class MissingKeyError(ScoutError):
    """No ScrapeCreators key was found (environment or macOS Keychain); only the free sources can run."""
