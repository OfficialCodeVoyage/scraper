"""Reading the sources' answers defensively: a field of the wrong type, or a list element that is not an object, is
skipped or read as empty instead of failing the whole answer (and, in a daily run, the calls already paid for).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

logger = logging.getLogger("scout.parsing")

T = TypeVar("T")
INT_LIMIT = 2**53
"""Counts are held inside this, which SQLite and every JSON reader take."""


def as_int(value: Any) -> int:
    """A count from an answer: 0 when it is not a number."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(-INT_LIMIT, min(INT_LIMIT, number))


def epoch(value: Any) -> datetime | None:
    """A UTC time from epoch seconds, None when it is not one (Infinity, 1e20 and text included)."""
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def iso(value: Any) -> datetime | None:
    """A time from an ISO 8601 string, in UTC when it names no zone; None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def field(body: Any, name: str) -> Any:
    """``body[name]`` when ``body`` is an object, else None."""
    return body.get(name) if isinstance(body, dict) else None


def objects(value: Any) -> list[dict[str, Any]]:
    """The objects of a list from an answer; anything else is no rows."""
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def parsed(rows: list[dict[str, Any]], parse: Callable[[dict[str, Any]], T | None], what: str) -> list[T]:
    """``parse`` of each row, skipping a row it returns None for or that is not the shape it reads."""
    out: list[T] = []
    for row in rows:
        try:
            found = parse(row)
        except (AttributeError, TypeError, ValueError, OverflowError, KeyError) as exc:
            logger.warning("skipped a malformed %s (%s)", what, type(exc).__name__)
            continue
        if found is not None:
            out.append(found)
    return out
