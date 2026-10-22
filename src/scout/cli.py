"""The ``scout`` command line.

    scout daily [--profile NAME] [--source NAME ...] [--config PATH] [--dry-run] [--no-claude] [--no-open] [--force]
    scout collect [--profile NAME] [--source NAME ...] [--config PATH] [--dry-run]
    scout brief [--date YYYY-MM-DD] [--profile NAME] [--config PATH] [--no-claude] [--no-open]
    scout credits [--json]
    scout search SOURCE QUERY [--limit N] [--since-days N] [--dry-run] [--json]
    scout user-posts SOURCE HANDLE [--limit N] [--dry-run] [--json]
    scout comments URL [--limit N] [--dry-run] [--json]
    scout transcript URL [--language CODE] [--dry-run] [--json]
    scout call ENDPOINT key=value ... [--allow-paid-params] [--dry-run] [--json]
    scout pageviews ARTICLE [--days N] [--json]
    scout endpoints [--json]
    scout profiles [--json]

``daily`` collects (paid calls inside the day's global credit cap, then the free sources) and writes the profile's
brief for today, then opens it without taking focus; a second ``daily`` the same day does nothing unless ``--force``.
``--dry-run`` lists the calls and their credits and calls nothing. ``--no-claude`` writes the brief from the ranked
items without summaries. The key is ``SCRAPECREATORS_API_KEY`` (the environment, else the macOS Keychain); without it
only the free sources run. Only ``daily`` and ``collect`` store what they find; the other commands print it.

Exit codes, the same with ``--json``: 0 done; 1 refused (a bad profile, a bad date, a refused parameter, no key, or
the daily cap refused the call, which was not sent); 2 a source failed in a way that stopped the command. A ``daily``
or ``collect`` run that stopped some paid calls at the cap still exits 0: it says what it skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from scout import __version__, api
from scout.config import SOURCES
from scout.models import ProviderError, ScoutError

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_FAILED = 2


def _day(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date like 2026-10-20") from None


def _pair(value: str) -> tuple[str, str]:
    name, sep, rest = value.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"{value!r} is not key=value")
    return name.strip(), rest


def build_parser() -> argparse.ArgumentParser:
    profile = argparse.ArgumentParser(add_help=False)
    profile.add_argument("--profile", default="default", help="the profile to run (default: default)")
    profile.add_argument("--config", default=None, help="read the profile from this file instead of --profile")
    sources = argparse.ArgumentParser(add_help=False)
    sources.add_argument(
        "--source", action="append", choices=SOURCES, help="run only this source (repeat for several); default all"
    )
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true", help="list the calls and credits, call nothing")
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--no-claude", action="store_true", help="no summaries: the brief lists the ranked items")
    output.add_argument("--no-open", action="store_true", help="do not open the brief in the browser")
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print JSON instead of readable lines")
    limit = argparse.ArgumentParser(add_help=False)
    limit.add_argument("--limit", type=int, default=20, help="at most this many results (default 20, max 100)")

    parser = argparse.ArgumentParser(
        prog="scout",
        description="Find what people post on Reddit, YouTube, X, Threads, Hacker News, GitHub, Bluesky and "
        "Wikipedia, under a daily credit cap.",
    )
    parser.add_argument("--version", action="version", version=f"scout {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    daily = commands.add_parser(
        "daily", parents=[profile, sources, dry, output], help="collect within the credit cap and write today's brief"
    )
    daily.add_argument("--force", action="store_true", help="run again although today's brief exists")
    commands.add_parser("collect", parents=[profile, sources, dry], help="collect and store, no brief")
    brief = commands.add_parser("brief", parents=[profile, output], help="build a day's brief again from the store")
    brief.add_argument("--date", type=_day, default=None, help="the day, YYYY-MM-DD (default today)")
    commands.add_parser("credits", parents=[as_json], help="the ScrapeCreators balance and today's use")

    search = commands.add_parser("search", parents=[limit, dry, as_json], help="search one source")
    search.add_argument("source", choices=api.SEARCH_SOURCES)
    search.add_argument("query")
    search.add_argument("--since-days", type=int, default=None, help="only the last N days (default: any time)")
    posts = commands.add_parser("user-posts", parents=[limit, dry, as_json], help="an account's latest posts")
    posts.add_argument("source", choices=api.USER_POST_SOURCES)
    posts.add_argument("handle", help="subreddit, channel handle or UC... id, X/Threads handle, Bluesky handle")
    comments = commands.add_parser(
        "comments", parents=[dry, as_json], help="the comments of a Reddit, YouTube, TikTok, Instagram or HN post"
    )
    comments.add_argument("url")
    comments.add_argument("--limit", type=int, default=50, help="at most this many comments (default 50, max 100)")
    transcript = commands.add_parser(
        "transcript", parents=[dry, as_json], help="a YouTube, TikTok, Instagram, X or Reddit video's transcript"
    )
    transcript.add_argument("url")
    transcript.add_argument("--language", default=None, help="a language code such as en (YouTube and TikTok)")
    call = commands.add_parser("call", parents=[dry, as_json], help="any allowlisted ScrapeCreators endpoint")
    call.add_argument("endpoint", help="an allowlisted path such as /v1/youtube/video/transcript")
    call.add_argument("params", nargs="*", type=_pair, metavar="key=value")
    call.add_argument(
        "--allow-paid-params",
        action="store_true",
        help="allow options that cost extra up to a known price (the endpoint's paid_params, e.g. get_ad_details=true); "
        "options with no upper price also need allow_open_ended_spend: true in config.yaml",
    )
    views = commands.add_parser("pageviews", parents=[as_json], help="daily Wikipedia page views of an article")
    views.add_argument("article", help="the English Wikipedia title, e.g. Theranos")
    views.add_argument("--days", type=int, default=30, help="the last N full days (default 30)")
    commands.add_parser("endpoints", parents=[as_json], help="the ScrapeCreators endpoints scout may call")
    commands.add_parser("profiles", parents=[as_json], help="the profiles in the config folder")
    return parser


# Output


def _dump(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _credit_note(result: dict[str, Any]) -> str:
    parts = [f"credits used {result.get('credits_used', 0)}"]
    if "credits_left_today" in result:
        parts.append(f"{result['credits_left_today']} of {result.get('daily_credit_cap')} left today")
    return "; ".join(parts)


def _print_plan(result: dict[str, Any]) -> None:
    print("Dry run, nothing was called:")
    for call in result.get("calls") or []:
        what = call.get("endpoint") or call.get("source")
        params = call.get("params") or {k: v for k, v in call.items() if k not in ("endpoint", "source", "credits")}
        print(f"  {what} {json.dumps(params, ensure_ascii=False)}: {call.get('credits', 0)} credit(s)")
    line = f"Credits needed: {result.get('credits_needed', 0)}"
    if "credits_left_today" in result:
        line += f"; {result['credits_left_today']} of {result.get('daily_credit_cap')} left today"
        if not result.get("key_found", True):
            line += "; no ScrapeCreators key found, so a real run would refuse"
    print(line)


def _one_line(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _print_items(result: dict[str, Any]) -> None:
    if result.get("skipped"):
        print(f"Skipped: {result.get('reason')}")
    for item in result.get("items") or []:
        stats = [f"{item.get('score', 0):,}"]
        if item.get("comments"):
            stats.append(f"{item['comments']:,} comments")
        when = (item.get("created_at") or "")[:10]
        head = f"[{item['source']}] {' · '.join(stats)}{' · ' + when if when else ''}"
        print(f"{head}  {_one_line(item.get('title') or item.get('text') or '', 100)}")
        print(f"    {item.get('url')}")
    if result.get("older_dropped"):
        print(f"{result['older_dropped']} older than --since-days left out")
    print(f"{result.get('count', 0)} items; {_credit_note(result)}")


def _print_comments(result: dict[str, Any]) -> None:
    if result.get("skipped"):
        print(f"Skipped: {result.get('reason')}")
    for comment in result.get("comments") or []:
        print(f"({comment.get('score', 0)}) {comment.get('author')}: {_one_line(comment.get('text') or '', 140)}")
    print(f"{result.get('count', 0)} comments; {_credit_note(result)}")


# Commands


def _run_profile(args: argparse.Namespace, *, write_brief: bool) -> int:
    common = {"sources": args.source, "dry_run": args.dry_run, "config_path": args.config}
    if write_brief:
        result = asyncio.run(
            api.daily(
                args.profile,
                use_claude=not args.no_claude,
                force=args.force,
                open_brief=not args.no_open,
                **common,
            )
        )
    else:
        result = asyncio.run(api.collect(args.profile, **common))
    if result.get("dry_run"):
        print("\n".join(result["plan"]))
        return EXIT_OK
    if result.get("status") == "exists":
        print(f"Today's brief exists: {result['brief_path']} (use --force to run again)")
        return EXIT_OK
    credit_line = result["credits"]
    print(
        f"Collected {result['items']} items with {result['paid_calls']} paid calls ({result['skipped']} skipped); "
        f"credits today {credit_line['spent']} of {credit_line['cap']}"
        + (f", {credit_line['balance']} left" if credit_line["balance"] is not None else "")
    )
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    if result.get("brief_path"):
        print(f"Brief: {result['brief_path']}")
    return EXIT_OK


def _brief(args: argparse.Namespace) -> int:
    result = asyncio.run(
        api.brief(
            args.date, args.profile, use_claude=not args.no_claude, open_brief=not args.no_open, config_path=args.config
        )
    )
    print(f"Brief: {result['brief_path']}")
    return EXIT_OK


def _credits(args: argparse.Namespace) -> int:
    result = asyncio.run(api.credits())
    if args.json:
        _dump(result)
        return EXIT_OK if result["key_found"] else EXIT_REFUSED
    if not result["key_found"]:
        print("error: SCRAPECREATORS_API_KEY is not set (environment or macOS Keychain)", file=sys.stderr)
        return EXIT_REFUSED
    print(f"ScrapeCreators balance: {result['balance']} credits")
    print(f"Used by scout today: {result['used_today']} of {result['daily_credit_cap']}")
    if result["used_elsewhere_since_last_check"]:
        print(f"Used elsewhere since the last check (MCP, other tools): {result['used_elsewhere_since_last_check']}")
    for row in result["by_endpoint"]:
        print(f"  {row['endpoint']}: {row['calls']} calls, {row['credits']} credits")
    return EXIT_OK


def _print_transcript(result: dict[str, Any]) -> None:
    if result.get("skipped"):
        print(f"Skipped: {result.get('reason')}")
    else:
        print(result.get("text") or "(no transcript)")
    print(_credit_note(result))


def _print_call(result: dict[str, Any]) -> None:
    if result.get("skipped"):
        print(f"Skipped: {result.get('reason')}")
    else:
        _dump(result.get("response"))
    print(_credit_note(result))


def _print_pageviews(result: dict[str, Any]) -> None:
    for row in result["items"]:
        print(f"{row['date']}  {row['views']:>9,}")
    print(f"{result['article']}: {result['total']:,} views in {result['count']} days, {result['daily_mean']:,} a day")


def _data_command(args: argparse.Namespace) -> int:
    printer: Callable[[dict[str, Any]], None]
    if args.command == "search":
        result = asyncio.run(api.search(args.source, args.query, args.limit, args.since_days, args.dry_run))
        printer = _print_items
    elif args.command == "user-posts":
        result = asyncio.run(api.user_posts(args.source, args.handle, args.limit, args.dry_run))
        printer = _print_items
    elif args.command == "comments":
        result = asyncio.run(api.comments(args.url, args.limit, args.dry_run))
        printer = _print_comments
    elif args.command == "transcript":
        result = asyncio.run(api.transcript(args.url, args.dry_run, args.language))
        printer = _print_transcript
    elif args.command == "call":
        result = asyncio.run(
            api.scrapecreators_call(args.endpoint, dict(args.params), args.dry_run, args.allow_paid_params)
        )
        printer = _print_call
    else:
        result = asyncio.run(api.wikipedia_pageviews(args.article, args.days))
        printer = _print_pageviews
    if args.json:
        _dump(result)
    elif result.get("dry_run"):
        _print_plan(result)
    else:
        printer(result)
    return EXIT_REFUSED if result.get("skipped") else EXIT_OK


def _listing(args: argparse.Namespace) -> int:
    if args.command == "endpoints":
        result = api.endpoints()
        if args.json:
            _dump(result)
            return EXIT_OK
        for row in result["endpoints"]:
            params = ", ".join(f"{p['name']}{'' if p['required'] else '?'}" for p in row["params"])
            price = f"{row['credits']}" + (f"-{row['credits_max']}" if row["credits_max"] else "")
            paid = f" paid options: {', '.join(row['paid_params'])}." if row["paid_params"] else ""
            note = f" Cost: {row['cost_note']}" if row["cost_note"] else ""
            print(f"{row['method']} {row['path']} ({price} credit(s)) [{params}]: {row['description']}{paid}{note}")
        return EXIT_OK
    result = api.profiles()
    if args.json:
        _dump(result)
        return EXIT_OK
    print(f"Profiles in {result['config_dir']}/profiles:")
    for row in result["profiles"]:
        if row["valid"]:
            print(f"  {row['name']}: {', '.join(row['lenses'])} (lens budgets {row['credit_budget']} credits)")
        else:
            print(f"  {row['name']}: does not load: {row['error']}")
    if not result["profiles"]:
        print("  none yet: copy examples/profiles/faceless-youtube.yaml there as default.yaml")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        if args.command == "daily":
            return _run_profile(args, write_brief=True)
        if args.command == "collect":
            return _run_profile(args, write_brief=False)
        if args.command == "brief":
            return _brief(args)
        if args.command == "credits":
            return _credits(args)
        if args.command in ("endpoints", "profiles"):
            return _listing(args)
        return _data_command(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except ScoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
