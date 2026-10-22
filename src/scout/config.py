"""The global config (``config.yaml``) and the profiles (``profiles/<name>.yaml``), validated by pydantic.

A misspelled key, an unknown lens, or lens budgets above the global daily cap are errors when the file loads, not a
surprise after credits are spent. The cap lives in the global config because one ledger counts every profile and
every ad-hoc call.

Open-ended spend (a paid option with no upper price at ScrapeCreators, such as ``allow_long_transcription``: 10
credits per started minute of video, with no duration limit) is off unless ``allow_open_ended_spend: true`` is set
here; no CLI flag or MCP argument can turn it on. When it is on, each such call reserves
``max_credits_per_open_ended_call`` and is refused when less than that is left today. ScrapeCreators may still bill
more than the reservation for a long video: the ledger then records what the answer says it cost (the day's total can
pass the cap), or keeps the reservation when no answer arrives.

A profile is the watch list of the daily brief: two lenses (``video_ideas`` and ``dev_intel``, the brief's two tabs,
either may be left out), each with a ``credit_budget``, ``watches`` (sentences sent to Claude: what to flag), an
optional ``prompt`` (a file path relative to the profile, or ``{system, user_template}`` inline; default: the
packaged prompt) and per source what to read. ``niche`` and ``brand_voice`` fill the prompts' ``{niche}``,
``{a_niche}`` and ``{brand_voice}``. ``known_tools`` adds to (or replaces) the packaged list of tools the dev-intel
tab knows; a tool a post names that is not on it is marked NEW.
"""

from __future__ import annotations

import re
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator, model_validator

from scout import __version__
from scout.paths import global_config_path, profile_path

SOURCES = ("reddit", "youtube", "x", "threads", "hn", "github", "bluesky")
PAID_SOURCES = ("reddit", "youtube", "x", "threads")
LENS_NAMES = ("video_ideas", "dev_intel")
DEFAULT_USER_AGENT = f"scout/{__version__} (+https://github.com/OfficialCodeVoyage/scraper)"
DEFAULT_CREDIT_CAP = 100
DEFAULT_OPEN_ENDED_CREDITS = 30
_VERSION_TAIL = re.compile(r"[\s-]+(v?\d[\w.]*|pro|max|lite|mini|flash|turbo|fast|plus|ultra|studio)$")
Source = Literal["reddit", "youtube", "x", "threads", "hn", "github", "bluesky"]

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(text: str) -> Any:
    """The parsed document in ``text`` (None for an empty one), with a safe loader."""
    return yaml.load(text, Loader=_Loader)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# Global config


class GlobalConfig(_Strict):
    daily_credit_cap: int = Field(default=DEFAULT_CREDIT_CAP, ge=0)
    user_agent: str = DEFAULT_USER_AGENT
    timezone: str | None = None
    allow_open_ended_spend: bool = False
    max_credits_per_open_ended_call: int = Field(default=DEFAULT_OPEN_ENDED_CREDITS, ge=10)

    @model_validator(mode="after")
    def _open_ended_fits_the_cap(self) -> Self:
        if self.allow_open_ended_spend and self.max_credits_per_open_ended_call > self.daily_credit_cap:
            raise ValueError(
                f"max_credits_per_open_ended_call ({self.max_credits_per_open_ended_call}) is above the "
                f"daily_credit_cap ({self.daily_credit_cap})"
            )
        return self

    @property
    def open_ended_limit(self) -> int | None:
        """What one open-ended call reserves, or None when open-ended spend is off."""
        return self.max_credits_per_open_ended_call if self.allow_open_ended_spend else None

    @field_validator("user_agent")
    @classmethod
    def _user_agent(cls, value: str) -> str:
        return value.strip() or DEFAULT_USER_AGENT

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown timezone {value!r} (use an IANA name like Europe/Berlin)") from None
        return value or None


def load_global_config(path: Path | None = None) -> GlobalConfig:
    """Read ``config.yaml``; a missing file gives the defaults.

    Raises:
        ValueError: a key is misspelled or a value is invalid.
    """
    path = path or global_config_path()
    try:
        data = load_yaml(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return GlobalConfig()
    return GlobalConfig(**data)


def local_now(config: GlobalConfig | None = None) -> datetime:
    """Now, in the configured timezone (else the machine's)."""
    tz = (config or load_global_config()).timezone
    return datetime.now(ZoneInfo(tz)) if tz else datetime.now().astimezone()


def today(config: GlobalConfig | None = None) -> str:
    """The calendar day the cap and the brief are counted by."""
    return local_now(config).date().isoformat()


# Profiles


class Subreddit(_Strict):
    name: str = Field(min_length=1)
    sort: Literal["best", "hot", "new", "top", "rising"] = "top"
    timeframe: Literal["all", "day", "week", "month", "year"] = "day"


class RedditSearch(_Strict):
    q: str = Field(min_length=1)
    timeframe: Literal["all", "day", "week", "month", "year"] = "week"


class RedditWatch(_Strict):
    subreddits: tuple[Subreddit, ...] = ()
    searches: tuple[RedditSearch, ...] = ()


class YouTubeChannel(_Strict):
    handle: str = Field(min_length=1)
    sort: Literal["latest", "popular"] = "latest"


class YouTubeSearch(_Strict):
    q: str = Field(min_length=1)
    upload_date: Literal["today", "this_week", "this_month", "this_year"] = "this_week"


class YouTubeWatch(_Strict):
    channels: tuple[YouTubeChannel, ...] = ()
    searches: tuple[YouTubeSearch, ...] = ()
    typeahead: tuple[str, ...] = ()


class XWatch(_Strict):
    searches: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()


class ThreadsWatch(_Strict):
    searches: tuple[str, ...] = ()


class HNWatch(_Strict):
    queries: tuple[str, ...] = ()
    min_points: int = Field(default=20, ge=0)


class GitHubWatch(_Strict):
    queries: tuple[str, ...] = ()
    min_stars: int = Field(default=30, ge=0)


class BlueskyWatch(_Strict):
    queries: tuple[str, ...] = ()


class InlinePrompt(_Strict):
    """A lens prompt written in the profile itself."""

    system: str = Field(min_length=1)
    user_template: str = Field(min_length=1)


class Lens(_Strict):
    """One tab of the brief and everything it watches."""

    label: str = Field(min_length=1)
    credit_budget: int = Field(ge=0)
    watches: tuple[str, ...] = ()
    prompt: str | InlinePrompt | None = None
    reddit: RedditWatch = RedditWatch()
    youtube: YouTubeWatch = YouTubeWatch()
    x: XWatch = XWatch()
    threads: ThreadsWatch = ThreadsWatch()
    hn: HNWatch = HNWatch()
    github: GitHubWatch = GitHubWatch()
    bluesky: BlueskyWatch = BlueskyWatch()


class CommentLimits(_Strict):
    """How many of the day's best items get their comments read (each Reddit or YouTube page costs a credit)."""

    top_posts: int = Field(default=30, ge=0)
    youtube_videos: int = Field(default=5, ge=0)
    hn_stories: int = Field(default=10, ge=0)


class SummaryLimits(_Strict):
    model: str = "claude-sonnet-5-5"
    max_items: int = Field(default=50, ge=1, le=120)
    timeout_seconds: int = Field(default=600, ge=30)


class KnownTool(_Strict):
    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    category: str = "other"


class KnownTools(_Strict):
    """Tools the dev-intel tab already knows: the packaged list (unless ``include_default`` is false) plus these."""

    include_default: bool = True
    tools: tuple[KnownTool, ...] = ()


class Profile(_Strict):
    niche: str = "general-interest"
    brand_voice: str = ""
    providers: dict[str, Literal["scrapecreators"]] = {"reddit": "scrapecreators"}
    comments: CommentLimits = CommentLimits()
    summary: SummaryLimits = SummaryLimits()
    wikipedia_entities: int = Field(default=10, ge=0)
    known_tools: KnownTools = KnownTools()
    lenses: dict[str, Lens]

    _name: str = PrivateAttr(default="")
    _path: Path | None = PrivateAttr(default=None)

    @field_validator("niche")
    @classmethod
    def _niche(cls, value: str) -> str:
        value = value.strip()
        if not value or "{" in value or "}" in value:
            raise ValueError("niche must be a non-empty phrase without braces")
        return value

    @model_validator(mode="after")
    def _budgets_fit_the_cap(self, info: ValidationInfo) -> Self:
        unknown = set(self.lenses) - set(LENS_NAMES)
        if unknown:
            raise ValueError(f"unknown lens {sorted(unknown)}: the brief has {', '.join(LENS_NAMES)}")
        if not self.lenses:
            raise ValueError(f"a profile needs at least one lens ({', '.join(LENS_NAMES)})")
        cap = (info.context or {}).get("daily_credit_cap") if info else None
        if isinstance(cap, int):
            lenses = sum(lens.credit_budget for lens in self.lenses.values())
            total = lenses + self.comments.top_posts + self.comments.youtube_videos
            if total > cap:
                raise ValueError(
                    f"the lenses' credit_budget ({lenses}) plus the comment calls "
                    f"({self.comments.top_posts} + {self.comments.youtube_videos}) is {total}, above the global "
                    f"daily_credit_cap ({cap})"
                )
        return self

    @property
    def name(self) -> str:
        return self._name

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def base_dir(self) -> Path:
        """The folder relative prompt paths are read from."""
        return self._path.parent if self._path else Path.cwd()


def load_profile(name: str = "default", *, path: Path | None = None, cap: int | None = None) -> Profile:
    """Read a profile by name (``<config>/profiles/<name>.yaml``) or from ``path``.

    ``cap`` is the global daily cap the lens budgets must fit (default: the global config's).

    Raises:
        FileNotFoundError: the file is missing.
        ValueError: a key is misspelled or a budget does not fit (pydantic's ``ValidationError`` is a ``ValueError``).
    """
    file = path or profile_path(name)
    data = load_yaml(file.read_text(encoding="utf-8")) or {}
    if cap is None:
        cap = load_global_config().daily_credit_cap
    profile = Profile.model_validate(data, context={"daily_credit_cap": cap})
    profile._name = name if path is None else file.stem
    profile._path = file.resolve()
    return profile


# Known tools


class ToolRegistry(_Strict):
    tools: tuple[KnownTool, ...] = ()

    def canonical(self, name: str) -> str | None:
        """The registry's name for ``name`` (case-insensitive, aliases included), or None for a tool it lacks.

        A version or tier at the end is ignored when the full name is not known: "Opus 5.5" and "Kling 3.0 Pro" match
        "Opus" and "Kling".
        """
        wanted = name.strip().casefold()
        while True:
            for tool in self.tools:
                if wanted == tool.name.casefold() or wanted in (a.casefold() for a in tool.aliases):
                    return tool.name
            shorter = _VERSION_TAIL.sub("", wanted)
            if shorter == wanted or not shorter:
                return None
            wanted = shorter


def default_known_tools() -> tuple[KnownTool, ...]:
    """The packaged list of known AI-video tools (``scout/data/known_tools.yaml``)."""
    text = resources.files("scout").joinpath("data/known_tools.yaml").read_text(encoding="utf-8")
    return ToolRegistry(**(load_yaml(text) or {})).tools


def tool_registry(profile: Profile) -> ToolRegistry:
    """The tools the profile's dev-intel tab knows."""
    base = default_known_tools() if profile.known_tools.include_default else ()
    return ToolRegistry(tools=(*base, *profile.known_tools.tools))
