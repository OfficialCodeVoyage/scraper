"""The daily run of a profile: collect within the credit cap, then build the brief from what is stored.

``collect`` makes the paid calls (ScrapeCreators, one lens at a time, each lens inside its ``credit_budget`` of credits
and all of them inside the global ``daily_credit_cap``) and the free ones, stores each call's items and a snapshot of
their counts as soon as it answers, ranks them to pick whose comments to read, and reads those. A source that fails,
or answers something scout cannot read, fails alone: the run notes a warning and goes on, and what the other calls
found (and paid for) stays stored. ``build_brief`` reads the day back from the profile's store, ranks
it, asks Claude for the cards (unless told not to), adds Wikipedia spikes to the video ideas and writes
``<profile data>/briefs/<day>.html``. At the cap, paid calls stop and the free sources and the brief still run; the
brief says what was skipped.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.claude_cli import ClaudeError
from scout.config import LENS_NAMES, PAID_SOURCES, Profile, ToolRegistry
from scout.keyless import KeylessSources
from scout.models import Comment, CreditCapReached, Item, ProviderError, ScoutError
from scout.paths import profile_data_dir
from scout.rank import YOUTUBE_MIN_OUTLIER, Ranked, outlier, question_demand, rank
from scout.report import render_brief, safe_url
from scout.scrapecreators import ScrapeCreatorsClient, endpoint
from scout.store import Store
from scout.summarise import demand_block, evidence_ids, summarise_lens

PaidRun = Callable[[ScrapeCreatorsClient], Awaitable[Any]]
FreeRun = Callable[[KeylessSources], Awaitable[Any]]


@dataclass
class PlannedCall:
    lens: str
    source: str
    label: str
    endpoint: str | None
    run: Callable[..., Awaitable[Any]]

    @property
    def credits(self) -> int:
        return endpoint(self.endpoint).credits if self.endpoint else 0


@dataclass
class Collection:
    items: list[Item] = field(default_factory=list)
    demand: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    paid_calls: int = 0
    skipped: int = 0
    credits_used: int = 0
    plan: list[str] = field(default_factory=list)
    plan_data: dict[str, Any] = field(default_factory=dict)


def brief_path(profile: str, day: str) -> Path:
    return profile_data_dir(profile) / "briefs" / f"{day}.html"


def paid_plan(profile: Profile, sources: set[str], demand: dict[str, list[str]]) -> list[PlannedCall]:
    """Every paid call of the day, lens by lens, in the order they are worth making."""
    calls: list[PlannedCall] = []

    async def typeahead(sc: ScrapeCreatorsClient, seed: str) -> list[Item]:
        demand[seed] = await sc.youtube_typeahead(seed)
        return []

    for name, lens in profile.lenses.items():
        planned: list[tuple[str, str, str, PaidRun]] = [
            *(
                (
                    "reddit",
                    f"r/{s.name}",
                    "/v1/reddit/subreddit",
                    lambda sc, s=s, name=name: sc.reddit_subreddit(s.name, s.sort, s.timeframe, name),
                )
                for s in lens.reddit.subreddits
            ),
            *(
                (
                    "youtube",
                    f"youtube {c.handle}",
                    "/v1/youtube/channel-videos",
                    lambda sc, c=c, name=name: sc.youtube_channel(c.handle, c.sort, name),
                )
                for c in lens.youtube.channels
            ),
            *(
                (
                    "reddit",
                    f"reddit '{r.q}'",
                    "/v1/reddit/search",
                    lambda sc, r=r, name=name: sc.reddit_search(r.q, r.timeframe, name),
                )
                for r in lens.reddit.searches
            ),
            *(
                (
                    "youtube",
                    f"typeahead '{seed}'",
                    "/v1/youtube/search/typeahead",
                    lambda sc, seed=seed: typeahead(sc, seed),
                )
                for seed in lens.youtube.typeahead
            ),
            *(
                (
                    "youtube",
                    f"youtube '{y.q}'",
                    "/v1/youtube/search",
                    lambda sc, y=y, name=name: sc.youtube_search(y.q, y.upload_date, name),
                )
                for y in lens.youtube.searches
            ),
            *(
                ("x", f"x '{q}'", "/v1/twitter/search/posts", lambda sc, q=q, name=name: sc.x_search(q, name))
                for q in lens.x.searches
            ),
            *(
                (
                    "x",
                    f"x @{h.lstrip('@')}",
                    "/v1/twitter/user-tweets",
                    lambda sc, h=h, name=name: sc.x_account(h, name),
                )
                for h in lens.x.accounts
            ),
            *(
                (
                    "threads",
                    f"threads '{q}'",
                    "/v1/threads/search",
                    lambda sc, q=q, name=name: sc.threads_search(q, name),
                )
                for q in lens.threads.searches
            ),
        ]
        calls += [PlannedCall(name, src, label, path, run) for src, label, path, run in planned if src in sources]
    return calls


def free_plan(profile: Profile, sources: set[str], now: datetime) -> list[PlannedCall]:
    calls: list[PlannedCall] = []
    recent = now - timedelta(days=3)
    week_ago = (now - timedelta(days=7)).date()
    for name, lens in profile.lenses.items():
        planned: list[tuple[str, str, FreeRun]] = [
            *(
                (
                    "hn",
                    f"hn '{q}'",
                    lambda free, q=q, lens=lens, name=name: free.hn_search(q, lens.hn.min_points, recent, name),
                )
                for q in lens.hn.queries
            ),
            *(
                (
                    "github",
                    f"github '{q}'",
                    lambda free, q=q, lens=lens, name=name: free.github_search(
                        q, lens.github.min_stars, week_ago, name
                    ),
                )
                for q in lens.github.queries
            ),
            *(
                (
                    "bluesky",
                    f"bluesky '{q}'",
                    lambda free, q=q, name=name: free.bluesky_search(q, recent, name),
                )
                for q in lens.bluesky.queries
            ),
        ]
        calls += [PlannedCall(name, src, label, None, run) for src, label, run in planned if src in sources]
    return calls


def describe_plan(
    profile: Profile, paid: list[PlannedCall], free_calls: list[PlannedCall], cap: int
) -> tuple[list[str], dict[str, Any]]:
    """The dry-run listing: text lines (the old CLI's format) and the same as data."""
    lines: list[str] = []
    lenses: dict[str, Any] = {}
    for name, lens in profile.lenses.items():
        mine = [c for c in paid if c.lens == name]
        lines.append(
            f"{name}: {len(mine)} paid calls (budget {lens.credit_budget})"
            + "".join(f"\n  {c.source}: {c.label}" for c in mine)
        )
        lenses[name] = {
            "credit_budget": lens.credit_budget,
            "paid_calls": [
                {"source": c.source, "label": c.label, "endpoint": c.endpoint, "credits": c.credits} for c in mine
            ],
            "paid_credits": sum(c.credits for c in mine),
            "paid_credits_within_budget": min(sum(c.credits for c in mine), lens.credit_budget),
        }
    comments = profile.comments
    lines.append(
        f"comments: up to {comments.top_posts} Reddit + {comments.youtube_videos} YouTube paid calls, "
        f"{comments.hn_stories} free HN"
    )
    lines.append(f"free calls: {len(free_calls)} (HN, GitHub, Bluesky); Claude: one call per lens")
    paid_credits = sum(c.credits for c in paid)
    total = paid_credits + comments.top_posts + comments.youtube_videos
    lines.append(f"paid total at most {total} of {cap} a day")
    data = {
        "lenses": lenses,
        "paid_calls": len(paid),
        "paid_credits": paid_credits,
        "comment_calls_max": comments.top_posts + comments.youtube_videos,
        "hn_comment_calls_max": comments.hn_stories,
        "free_calls": [{"source": c.source, "label": c.label, "lens": c.lens} for c in free_calls],
        "credits_max": total,
        "daily_credit_cap": cap,
    }
    return lines, data


async def collect(
    profile: Profile,
    store: Store,
    sc: ScrapeCreatorsClient | None,
    free: KeylessSources,
    *,
    sources: set[str],
    now: datetime,
    cap: int,
    dry_run: bool = False,
) -> Collection:
    """Fetch the day's items and the comments of the best of them; store everything."""
    result = Collection()
    paid = paid_plan(profile, sources if (sc or dry_run) else sources - set(PAID_SOURCES), result.demand)
    free_calls = free_plan(profile, sources, now)
    if dry_run:
        result.plan, result.plan_data = describe_plan(profile, paid, free_calls, cap)
        return result

    def keep(label: str, found: list[Item]) -> None:
        """Store one call's items at once, so a later failure cannot lose what was already paid for."""
        try:
            store.save_items(found, now)
        except Exception as exc:  # noqa: BLE001 -- one bad answer must not stop the run
            result.warnings.append(f"{label}: its items could not be stored ({_failure(exc)})")
            return
        result.items.extend(found)

    async def run_free(call: PlannedCall) -> list[Item]:
        try:
            return await call.run(free)
        except ProviderError as exc:
            result.warnings.append(f"{call.label}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- one bad source fails alone
            result.warnings.append(f"{call.label}: {_failure(exc)}")
        return []

    free_task = asyncio.gather(*(run_free(c) for c in free_calls))
    used = dict.fromkeys(profile.lenses, 0)
    for call in paid:
        if sc is None:
            break
        if used[call.lens] + call.credits > profile.lenses[call.lens].credit_budget:
            result.skipped += 1
            continue
        before = sc.credits_used
        found: list[Item] = []
        try:
            found = await call.run(sc)
            result.paid_calls += 1
        except CreditCapReached as exc:
            result.skipped += 1
            if not any(w.startswith("Credit cap") for w in result.warnings):
                result.warnings.append(f"Credit cap: {exc}; the remaining paid calls were skipped")
        except ProviderError as exc:
            result.warnings.append(f"{call.label}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- one bad source fails alone; its credits are already counted
            result.warnings.append(f"{call.label}: {_failure(exc)}")
        used[call.lens] += sc.credits_used - before
        if found:
            keep(call.label, found)
    for call, found in zip(free_calls, await free_task, strict=True):
        if found:
            keep(call.label, found)

    for comment_call in _comment_calls(profile, store, sc, free, result.items, now):
        try:
            store.save_comments(await comment_call.run())
            if comment_call.source != "hn":
                result.paid_calls += 1
        except CreditCapReached:
            result.skipped += 1
        except ProviderError as exc:
            result.warnings.append(f"{comment_call.label}: {exc}")
        except Exception as exc:  # noqa: BLE001 -- one bad thread fails alone
            result.warnings.append(f"{comment_call.label}: {_failure(exc)}")
    result.credits_used = sc.credits_used if sc else 0
    return result


def _failure(exc: BaseException) -> str:
    """A short note for a failure that is not a ``ProviderError``: an answer scout could not read."""
    return f"failed on an answer scout could not read ({type(exc).__name__})"


@dataclass
class _CommentCall:
    source: str
    label: str
    run: Callable[[], Awaitable[list[Comment]]]


def _comment_calls(
    profile: Profile,
    store: Store,
    sc: ScrapeCreatorsClient | None,
    free: KeylessSources,
    items: list[Item],
    now: datetime,
) -> list[_CommentCall]:
    """Comments of the day's best: Reddit posts with talk under them, YouTube outliers, HN stories (free)."""
    ranked = [entry.item for lens in profile.lenses for entry in rank([i for i in items if i.lens == lens], store, now)]
    best = list({item.key: item for item in ranked}.values())  # in rank order; a post both lenses found, once
    reddit = [i for i in best if i.source == "reddit" and i.comments >= 5][: profile.comments.top_posts]
    youtube = [
        i for i in best if i.source == "youtube" and i.comments >= 5 and (outlier(i) or 0) >= YOUTUBE_MIN_OUTLIER
    ][: profile.comments.youtube_videos]
    hn = [i for i in best if i.source == "hn" and i.comments >= 5][: profile.comments.hn_stories]
    calls: list[_CommentCall] = []
    if sc is not None:
        client = sc
        calls += [_CommentCall("reddit", f"comments {i.url}", lambda i=i: client.reddit_comments(i)) for i in reddit]
        calls += [_CommentCall("youtube", f"comments {i.url}", lambda i=i: client.youtube_comments(i)) for i in youtube]
    calls += [_CommentCall("hn", f"comments {i.url}", lambda i=i: free.hn_comments(i)) for i in hn]
    return calls


def _stats(item: Item) -> str:
    unit = {"reddit": "upvotes", "hn": "points", "github": "stars", "youtube": "views"}.get(item.source, "likes")
    parts = [f"{item.score:,} {unit}"]
    if item.comments:
        parts.append(f"{item.comments:,} comments")
    if item.views and item.source != "youtube":
        parts.append(f"{item.views:,} views")
    multiple = item.extra.get("outlier")
    if multiple:
        parts.append(f"{multiple}x channel median")
    if item.created_at:
        parts.append(item.created_at.date().isoformat())
    return " · ".join(parts)


def _evidence_row(entry: Ranked) -> dict[str, Any]:
    item = entry.item
    where = f"r/{item.community}" if item.source == "reddit" else f"{item.source} {item.community}".strip()
    if item.source in ("hn", "github", "bluesky", "threads", "x"):
        where = item.source if item.community == item.source else where
    return {
        "label": item.title or item.text[:140] or item.url,
        "url": safe_url(item.url),
        "where": where,
        "stats": _stats(item),
    }


async def build_brief(
    profile: Profile,
    store: Store,
    registry: ToolRegistry,
    free: KeylessSources,
    *,
    day: str,
    now: datetime,
    demand: dict[str, list[str]],
    warnings: list[str],
    use_claude: bool,
    credits: dict[str, Any],
) -> Path:
    """Rank the day's stored items, summarise them, write the HTML brief; return its path."""
    lenses = [lens for lens in LENS_NAMES if lens in profile.lenses]
    items = store.items_of_day(day)
    ranked = {lens: rank([i for i in items if i.lens == lens], store, now) for lens in lenses}
    comments: dict[str, list[Comment]] = {}
    for entries in ranked.values():
        for entry in entries[: profile.summary.max_items]:
            thread = store.comments_for(entry.item)
            if thread:
                comments[entry.item.key] = thread
                demand_stats = question_demand(thread)
                if demand_stats["questions"]:
                    entry.signals["questions"] = demand_stats["questions"]

    watches = {lens: "\n".join(f"- {w}" for w in profile.lenses[lens].watches) or "(none)" for lens in lenses}
    seeds = profile.lenses["video_ideas"].youtube.typeahead if "video_ideas" in profile.lenses else ()
    demand_lines = list(dict.fromkeys(line for seed in seeds for line in demand.get(seed, [])))
    cards: dict[str, list[dict[str, Any]]] = {lens: [] for lens in LENS_NAMES}
    made_with = "no summaries (--no-claude)"
    if use_claude:
        manifest = profile_data_dir(profile.name) / "runs" / day / "manifest.json"
        models: set[str] = set()
        for lens in lenses:
            if not ranked[lens]:
                continue
            context = {"date": day, "watches": watches[lens]}
            if lens == "video_ideas":
                context["demand"] = demand_block(demand_lines[:80])
            try:
                found, note = await summarise_lens(
                    lens,
                    ranked[lens],
                    comments,
                    store=store,
                    config=profile,
                    day=day,
                    context=context,
                    manifest=manifest,
                )
                cards[lens] = found
                models.update(note.get("models") or [])
            except (ClaudeError, ScoutError, OSError, ValueError, KeyError) as exc:
                warnings.append(f"{lens} summary failed, showing the ranked items instead: {exc}")
        made_with = f"summaries by {', '.join(sorted(models)) or profile.summary.model} via claude -p"

    ids = {lens: evidence_ids(ranked[lens][: profile.summary.max_items]) for lens in lenses}
    for lens in lenses:
        kept = []
        for card in cards[lens]:
            if card.get("junk"):
                continue
            entries = [ids[lens][e] for e in card["evidence"]]
            card["final"] = round(max(e.signal for e in entries) * (0.5 + card["worthiness"] / 100), 2)
            card["evidence_items"] = [_evidence_row(e) for e in entries]
            kept.append(card)
        # Fit and risk before heat: a high-risk idea goes below every other, however fast its posts rise.
        cards[lens] = sorted(kept, key=lambda c: (c.get("policy_risk") != "high", c["final"]), reverse=True)

    await _add_wikipedia(cards["video_ideas"], free, profile, now, warnings)
    if "dev_intel" in ids:
        _flag_new_tools(cards["dev_intel"], ids["dev_intel"], registry, store, day)

    sources: dict[str, int] = {}
    for item in items:
        sources[item.source] = sources.get(item.source, 0) + 1
    html = render_brief(
        {
            "day": day,
            "profile": profile.name,
            "credits": credits,
            "counts": {
                "total": len(items),
                "sources": ", ".join(f"{name} {count}" for name, count in sorted(sources.items())) or "no sources",
            },
            "made_with": made_with,
            "warnings": warnings,
            "ideas": cards["video_ideas"],
            "dev": cards["dev_intel"],
            "top": {lens: [_evidence_row(e) for e in ranked.get(lens, [])[:30]] for lens in LENS_NAMES},
            "demand": demand_lines,
            "footer_note": f"Built {now.strftime('%Y-%m-%d %H:%M')}.",
        }
    )
    path = brief_path(profile.name, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    store.save_run(
        day,
        path,
        {
            "now": now.isoformat(),
            "demand": demand,
            "warnings": warnings,
            "credits": credits,
            "cards": {k: len(v) for k, v in cards.items()},
        },
    )
    return path


async def _add_wikipedia(
    cards: list[dict[str, Any]], free: KeylessSources, profile: Profile, now: datetime, warnings: list[str]
) -> None:
    """Page-view spikes of the top ideas' companies and people (free)."""
    budget = profile.wikipedia_entities
    for card in cards:
        card["wiki"] = []
        for name in (card.get("entities") or [])[:2]:
            if budget <= 0:
                return
            budget -= 1
            try:
                spike = await free.wikipedia_spike(str(name), now.date())
            except ProviderError as exc:
                warnings.append(f"Wikipedia '{name}': {exc}")
                continue
            if spike:
                card["wiki"].append(spike)


def _flag_new_tools(
    cards: list[dict[str, Any]], ids: dict[str, Ranked], registry: ToolRegistry, store: Store, day: str
) -> None:
    """Mark the tools the registry does not know, and remember who mentioned them (``tool_mentions``)."""
    week_ago = (datetime.fromisoformat(day) - timedelta(days=7)).date().isoformat()
    for card in cards:
        mentions = []
        for tool in card.get("tools") or []:
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            known = registry.canonical(name)
            tool["new"] = known is None
            canonical = known or name
            for evidence in card["evidence"]:
                item = ids[evidence].item
                mentions.append((canonical, item.source, item.ext_id, item.author))
        store.save_tool_mentions(day, mentions)
        for tool in card.get("tools") or []:
            if tool.get("new"):
                tool["authors"] = store.tool_authors(str(tool.get("name")), week_ago)


def open_quietly(path: Path) -> None:
    """Open the brief in the default browser, on macOS without taking focus (``open -g``)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-g", str(path)], check=False, timeout=10)
        else:
            webbrowser.open(path.as_uri())
    except (OSError, subprocess.SubprocessError, webbrowser.Error):
        pass
