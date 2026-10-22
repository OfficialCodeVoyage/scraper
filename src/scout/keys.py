"""The ScrapeCreators key: the environment, else the macOS Keychain. Never a file.

``SCRAPECREATORS_API_KEY`` in the environment wins; on macOS the login Keychain's generic password of the same
service name is read with ``security find-generic-password -s SCRAPECREATORS_API_KEY -w``. The key is never logged,
printed or put in an exception message, and no ``.env``-style file is ever read. Without a key only the free sources
run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

KEY_NAME = "SCRAPECREATORS_API_KEY"


def _from_keychain(name: str) -> str | None:
    if sys.platform != "darwin":
        return None
    security = shutil.which("security") or "/usr/bin/security"
    try:
        found = subprocess.run(
            [security, "find-generic-password", "-s", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0:
        return None
    value = found.stdout.strip()
    return value or None


def get_api_key(name: str = KEY_NAME) -> str | None:
    """The key, or None when neither the environment nor the Keychain has one."""
    value = os.environ.get(name, "").strip()
    return value or _from_keychain(name)


def key_source(name: str = KEY_NAME) -> str | None:
    """Where the key would come from (``environment`` or ``keychain``), without returning it."""
    if os.environ.get(name, "").strip():
        return "environment"
    return "keychain" if _from_keychain(name) else None
