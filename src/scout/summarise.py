"""Turning a lens's ranked items into the brief's cards with one ``claude -p`` call per lens.

Each lens is one shard of the day's manifest (``<profile data>/runs/<day>/manifest.json``); with one subscription the
shards run one after the other, and a shard whose input was answered before comes from the store, free. Post text and
YouTube autocomplete lines are untrusted: they go in ``<item>`` and ``<demand>`` blocks with every ``<`` escaped (and
attribute values fully escaped), the system prompt says they are data, and an answer may only cite the evidence ids it
was given (others are dropped). Prompts: the profile's, else ``scout/data/prompts/``.
"""

from __future__ import annotations

import hashlib
import html
import json
import time
from pathlib import Path
from typing import Any

from scout.claude_cli import ask_claude
from scout.config import Profile
from scout.models import Comment
from scout.prompts import PromptTemplate, load_prompt
from scout.rank import Ranked, question_demand
from scout.store import Store

ITEM_TEXT_CHARS = 700
COMMENT_CHARS = 280
COMMENTS_PER_ITEM = 5

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

VIDEO_IDEAS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ideas"],
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "thesis",
                    "why_now",
                    "hooks",
                    "questions",
                    "entities",
                    "evidence",
                    "worthiness",
                    "policy_risk",
                    "junk",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "thesis": {"type": "string"},
                    "why_now": {"type": "string"},
                    "hooks": _STRING_LIST,
                    "questions": _STRING_LIST,
                    "entities": _STRING_LIST,
                    "evidence": _STRING_LIST,
                    "worthiness": {"type": "integer", "minimum": 0, "maximum": 100},
                    "policy_risk": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "junk": {"type": "boolean"},
                },
            },
        }
    },
}

DEV_INTEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "kind",
                    "summary",
                    "steps",
                    "tools",
                    "stage",
                    "action",
                    "evidence",
                    "worthiness",
                    "junk",
                ],
                "properties": {
                    "title": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "new_tool",
                            "release",
                            "how_to",
                            "watch_match",
                            "technique",
                            "warning",
                            "discussion",
                        ],
                    },
                    "summary": {"type": "string"},
                    "steps": _STRING_LIST,
                    "tools": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "stance"],
                            "properties": {
                                "name": {"type": "string"},
                                "stance": {
                                    "type": "string",
                                    "enum": [
                                        "recommends",
                                        "warns",
                                        "asks",
                                        "compares",
                                        "announces",
                                    ],
                                },
                            },
                        },
                    },
                    "stage": {"type": "string"},
                    "action": {"type": "string"},
                    "evidence": _STRING_LIST,
                    "worthiness": {"type": "integer", "minimum": 0, "maximum": 100},
                    "junk": {"type": "boolean"},
                },
            },
        }
    },
}

SCHEMAS = {"video_ideas": VIDEO_IDEAS_SCHEMA, "dev_intel": DEV_INTEL_SCHEMA}
CARD_LISTS = {"video_ideas": "ideas", "dev_intel": "items"}


def _fence(text: str, limit: int) -> str:
    """Untrusted text, cut to ``limit`` characters, with every ``<`` escaped so it cannot close or open a block."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat.replace("<", "&lt;")


def _attr(text: str, limit: int) -> str:
    """Untrusted text for an attribute value: cut like ``_fence`` and with ``&``, ``<``, ``>`` and quotes escaped."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return html.escape(flat, quote=True)


def demand_block(lines: list[str]) -> str:
    """YouTube autocomplete lines as a ``<demand>`` data block, each escaped like post text."""
    if not lines:
        return "<demand>\n(none today)\n</demand>"
    return "<demand>\n" + "\n".join(f"- {_fence(line, 200)}" for line in lines) + "\n</demand>"


def evidence_ids(ranked: list[Ranked]) -> dict[str, Ranked]:
    return {f"E{n:02d}": entry for n, entry in enumerate(ranked, start=1)}


def render_items(ids: dict[str, Ranked], comments: dict[str, list[Comment]]) -> str:
    blocks: list[str] = []
    for evidence_id, entry in ids.items():
        item = entry.item
        signals = ", ".join(f"{k}={v}" for k, v in entry.signals.items())
        where = f"r/{item.community}" if item.source == "reddit" else item.community
        lines = [
            f'<item id="{evidence_id}" source="{_attr(item.source, 20)}" where="{_attr(where, 60)}" '
            f'signals="{_attr(signals, 200)}">',
            f"title: {_fence(item.title, 200)}",
        ]
        if item.text and item.text.strip() != item.title.strip():
            lines.append(f"text: {_fence(item.text, ITEM_TEXT_CHARS)}")
        thread = comments.get(item.key) or []
        if thread:
            demand = question_demand(thread)
            lines.append(f"comments ({demand['questions']} questions, {demand['gold']} with 5+ likes):")
            for comment in thread[:COMMENTS_PER_ITEM]:
                lines.append(f"- ({comment.score} likes) {_fence(comment.text, COMMENT_CHARS)}")
        lines.append("</item>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def prompt_for(lens: str, profile: Profile) -> PromptTemplate:
    return load_prompt(lens, profile)


def input_hash(template: PromptTemplate, user: str) -> str:
    return hashlib.sha256(f"{template.version_hash}\n{user}".encode()).hexdigest()[:16]


def clean_cards(lens: str, data: dict[str, Any], ids: dict[str, Ranked]) -> list[dict[str, Any]]:
    """The answer's cards, keeping only evidence ids that were in the input; a card left with none is dropped."""
    cards: list[dict[str, Any]] = []
    for card in data.get(CARD_LISTS[lens]) or []:
        if not isinstance(card, dict):
            continue
        evidence = [e for e in card.get("evidence") or [] if e in ids]
        if not evidence:
            continue
        card["evidence"] = evidence
        card["worthiness"] = max(0, min(100, int(card.get("worthiness") or 50)))
        cards.append(card)
    return cards


async def summarise_lens(
    lens: str,
    ranked: list[Ranked],
    comments: dict[str, list[Comment]],
    *,
    store: Store,
    config: Profile,
    day: str,
    context: dict[str, str],
    manifest: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The lens's cards and a note for the manifest; cached by the exact prompt and input."""
    ids = evidence_ids(ranked[: config.summary.max_items])
    template = prompt_for(lens, config)
    user = template.format_user(items=render_items(ids, comments), **context)
    key = input_hash(template, user)
    note: dict[str, Any] = {
        "lens": lens,
        "items": len(ids),
        "input_hash": key,
        "prompt": template.version_hash,
    }
    cached = store.cached_summary(lens, key)
    if cached is not None:
        note["status"] = "cached"
        _write_manifest(manifest, note)
        return clean_cards(lens, cached, ids), note
    started = time.monotonic()
    answer = await ask_claude(
        user,
        system=template.format_system(**context),
        schema=SCHEMAS[lens],
        model=config.summary.model,
        timeout=config.summary.timeout_seconds,
    )
    store.save_summary(lens, key, day, template.version_hash, answer.data)
    note.update(
        status="answered",
        models=list(answer.models),
        seconds=round(time.monotonic() - started),
    )
    _write_manifest(manifest, note)
    return clean_cards(lens, answer.data, ids), note


def _write_manifest(path: Path, note: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shards = json.loads(path.read_text()) if path.is_file() else {"shards": []}
    shards["shards"] = [s for s in shards["shards"] if s.get("lens") != note["lens"]] + [note]
    path.write_text(json.dumps(shards, indent=2))
