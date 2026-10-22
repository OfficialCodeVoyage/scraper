"""Lens prompts: a system prompt and a user template, from a profile, a file or the package.

A prompt file is YAML: either ``{system, user_template}`` or the variant form ``{name, description, variants: [{id,
weight, system, user_template}]}`` (a variant is picked by weight). ``{niche}``, ``{a_niche}`` and ``{brand_voice}``
are filled from the profile when the prompt loads, before the version hash is taken, so the hash follows the text the
model sees; the remaining ``{placeholders}`` are filled per call. Values are substituted in one pass, so braces inside
a value (post text, JSON) are left alone.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from scout.config import InlinePrompt, Profile, load_yaml

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    variant_id: str
    system_prompt: str
    user_template: str
    version_hash: str

    @property
    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.system_prompt + self.user_template))

    def format_user(self, **kwargs: str) -> str:
        """The user template with every placeholder filled.

        Raises:
            KeyError: a placeholder of the template has no value in ``kwargs``.
        """
        missing = set(_PLACEHOLDER.findall(self.user_template)) - set(kwargs)
        if missing:
            raise KeyError(f"prompt {self.name}: no value for {sorted(missing)}")
        return _PLACEHOLDER.sub(lambda m: str(kwargs[m.group(1)]), self.user_template)

    def format_system(self, **kwargs: str) -> str:
        """The system prompt with the placeholders ``kwargs`` provides filled; others are left as they are."""
        return _PLACEHOLDER.sub(
            lambda m: str(kwargs[m.group(1)]) if m.group(1) in kwargs else m.group(0),
            self.system_prompt,
        )


def with_article(phrase: str) -> str:
    """``economic history`` -> ``an economic history``."""
    return f"{'an' if phrase[:1].lower() in 'aeiou' else 'a'} {phrase}"


def profile_vars(profile: Profile) -> dict[str, str]:
    return {
        "niche": profile.niche,
        "a_niche": with_article(profile.niche),
        "brand_voice": profile.brand_voice.strip() or "(none given)",
    }


def _fill(text: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)


def _pick(data: dict[str, Any]) -> tuple[str, str, str]:
    """(variant id, system, user_template) from a prompt document."""
    if "variants" in data:
        variants = data["variants"]
        if not isinstance(variants, list) or not variants:
            raise ValueError("a prompt's variants must be a non-empty list")
        chosen = random.choices(variants, weights=[v.get("weight", 1) for v in variants], k=1)[0]
        return str(chosen.get("id", "v1")), str(chosen["system"]), str(chosen["user_template"])
    return "v1", str(data["system"]), str(data["user_template"])


def _document(lens: str, spec: str | InlinePrompt | None, base_dir: Path) -> tuple[str, dict[str, Any]]:
    if isinstance(spec, InlinePrompt):
        return lens, {"system": spec.system, "user_template": spec.user_template}
    if spec is None:
        text = resources.files("scout").joinpath(f"data/prompts/{lens}.yaml").read_text(encoding="utf-8")
    else:
        path = Path(spec).expanduser()
        text = (path if path.is_absolute() else base_dir / path).read_text(encoding="utf-8")
    data = load_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"the {lens} prompt is not a YAML mapping")
    return str(data.get("name") or lens), data


def load_prompt(lens: str, profile: Profile) -> PromptTemplate:
    """The prompt of ``lens`` for ``profile``: its own (inline or a file), else the packaged one.

    Raises:
        FileNotFoundError: the prompt file is missing.
        ValueError: the file is not a prompt document.
    """
    name, data = _document(lens, profile.lenses[lens].prompt, profile.base_dir)
    try:
        variant_id, system, user = _pick(data)
    except KeyError as exc:
        raise ValueError(f"the {lens} prompt has no {exc.args[0]}") from None
    values = profile_vars(profile)
    system, user = _fill(system, values), _fill(user, values)
    digest = hashlib.sha256(f"{variant_id}\n{system}\n{user}".encode()).hexdigest()[:8]
    return PromptTemplate(
        name=name, variant_id=variant_id, system_prompt=system, user_template=user, version_hash=digest
    )
