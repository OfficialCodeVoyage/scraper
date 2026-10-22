"""``claude -p`` on the user's Claude subscription, for the brief's summaries; never the API.

The child gets a short allowlist of environment variables and no ``ANTHROPIC_*`` or ``*_KEY``: with an API key in its
environment ``claude`` would bill the API instead of the subscription. It runs in an empty folder with every tool off
and ``--safe-mode`` (no CLAUDE.md, hooks, skills, plugins or MCP servers, so the ScrapeCreators MCP cannot spend
credits from here), keeps no session, and is killed at the timeout. The answer is the ``structured_output`` of
``--output-format json`` validated by ``--json-schema``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scout.models import ScoutError

# CLAUDE_CONFIG_DIR says where Claude Code keeps its login; it is a folder, not a key.
_ENV_ALLOWLIST = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CLAUDE_CONFIG_DIR")


class ClaudeError(ScoutError):
    """``claude -p`` failed, timed out or answered without the structured output."""


@dataclass(frozen=True)
class ClaudeAnswer:
    data: dict[str, Any]
    models: tuple[str, ...]
    seconds: float


def claude_binary() -> str:
    """The ``claude`` command: on PATH, else the installer's ``~/.local/bin/claude``."""
    found = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not Path(found).exists():
        raise ClaudeError("the claude command was not found (install Claude Code or put it on PATH)")
    return found


def child_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    path = os.environ.get("PATH", "/usr/bin:/bin")
    local_bin = str(Path.home() / ".local" / "bin")
    env["PATH"] = path if local_bin in path.split(":") else f"{local_bin}:{path}"
    return env


async def ask_claude(prompt: str, *, system: str, schema: dict[str, Any], model: str, timeout: float) -> ClaudeAnswer:
    """Send ``prompt`` (on stdin) with ``system`` and return the answer that matches ``schema``.

    Raises:
        ClaudeError: the command failed, timed out, reported an error, or gave no structured output.
    """
    args = [
        claude_binary(),
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "--system-prompt",
        system,
    ]
    with tempfile.TemporaryDirectory(prefix="scout-claude-") as folder:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=folder,
            env=child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(prompt.encode()), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ClaudeError(f"claude -p took longer than {timeout:.0f} s and was stopped") from None
    if process.returncode != 0:
        raise ClaudeError(f"claude -p exited {process.returncode}: {err.decode(errors='replace')[-400:].strip()}")
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise ClaudeError("claude -p printed something that is not JSON") from exc
    data = result.get("structured_output")
    if result.get("is_error") or not isinstance(data, dict):
        raise ClaudeError(f"claude -p gave no structured answer: {str(result.get('result'))[:300]}")
    return ClaudeAnswer(
        data=data,
        models=tuple((result.get("modelUsage") or {}).keys()),
        seconds=float(result.get("duration_ms") or 0) / 1000,
    )
