"""Agent Card lint (ADR-0036). Deterministic; runs at registration and refresh.

Card free text never reaches an LLM (ADR-0008); lint additionally rejects cards whose text
looks like it is aimed at one, hides characters, carries markup, or points off-vendor.

HARD (registration rejected):
- LINT_INJECTION: instruction-like markers or tool/function-call syntax; base64-like runs of
  more than 64 characters.
- HIDDEN_UNICODE: zero-width, bidi control or tag characters.
- MARKUP: HTML/XML tags.
- FOREIGN_URL: a URL in free text, documentationUrl or iconUrl whose host is not the vendor
  domain or a subdomain of it.
SOFT (accepted, warning recorded): long description, long skill description, many or long
examples.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from bourse.contracts.registry import RejectReason

DEFAULT_MARKERS: tuple[str, ...] = (
    r"\bignore\s+(all\s+)?(the\s+)?previous\b",
    r"\bsystem\s+prompt\b",
    r"\byou\s+must\b",
    r"\bdisregard\b",
    r"<\|[^|>]{0,40}\|>",  # chat-template control tokens
    r"\b(function_call|tool_calls?|tool_use)\b",
    r"\{\s*\"(name|arguments|function)\"\s*:",
    r"^\s*(Action|Action Input|Observation|Thought)\s*:",
)
HIDDEN = re.compile("[​-‏⁠-⁤﻿‪-‮⁦-⁩\U000e0000-\U000e007f]")
TAG = re.compile(r"<\s*/?\s*[A-Za-z][A-Za-z0-9:-]*(\s[^<>]*)?/?\s*>")
URL = re.compile(r"\bhttps?://[^\s\"'<>()]+", re.IGNORECASE)
BASE64_RUN = re.compile(r"[A-Za-z0-9+/=_-]{65,}")

MAX_DESCRIPTION = 500
MAX_SKILL_DESCRIPTION = 300
MAX_EXAMPLES = 5
MAX_EXAMPLE_CHARS = 200


@dataclass(frozen=True)
class LintFinding:
    reason: RejectReason | None  # None for SOFT warnings
    field: str
    detail: str


@dataclass(frozen=True)
class LintResult:
    hard: tuple[LintFinding, ...] = ()
    soft: tuple[LintFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.hard


@dataclass(frozen=True)
class LintConfig:
    markers: Sequence[str] = field(default=DEFAULT_MARKERS)


def host_on_vendor(host: str, vendor_domain: str) -> bool:
    host = host.lower().rstrip(".")
    return host == vendor_domain or host.endswith("." + vendor_domain)


def _texts(card: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for key in ("name", "description"):
        value = card.get(key)
        if isinstance(value, str):
            yield key, value
    provider = card.get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("organization"), str):
        yield "provider.organization", provider["organization"]
    for i, skill in enumerate(card.get("skills") or []):
        if not isinstance(skill, dict):
            continue
        for key in ("id", "name", "description"):
            if isinstance(skill.get(key), str):
                yield f"skills[{i}].{key}", skill[key]
        for j, tag in enumerate(skill.get("tags") or []):
            if isinstance(tag, str):
                yield f"skills[{i}].tags[{j}]", tag
        for j, example in enumerate(skill.get("examples") or []):
            if isinstance(example, str):
                yield f"skills[{i}].examples[{j}]", example


def lint_card(
    card: dict[str, Any], vendor_domain: str, config: LintConfig | None = None
) -> LintResult:
    cfg = config or LintConfig()
    markers = [re.compile(m, re.IGNORECASE | re.MULTILINE) for m in cfg.markers]
    hard: list[LintFinding] = []
    soft: list[LintFinding] = []

    def flag(reason: RejectReason, where: str, detail: str) -> None:
        hard.append(LintFinding(reason, where, detail))

    for where, text in _texts(card):
        if HIDDEN.search(text):
            flag(RejectReason.HIDDEN_UNICODE, where, "zero-width, bidi or tag character")
        for m in markers:
            if m.search(text):
                flag(RejectReason.LINT_INJECTION, where, f"marker /{m.pattern}/")
                break
        if BASE64_RUN.search(text):
            flag(RejectReason.LINT_INJECTION, where, "base64-like run > 64 chars")
        if TAG.search(text):
            flag(RejectReason.MARKUP, where, "HTML/XML tag")
        for url in URL.findall(text):
            host = urlsplit(url).hostname or ""
            if not host_on_vendor(host, vendor_domain):
                flag(RejectReason.FOREIGN_URL, where, f"host {host!r}")
    for key in ("documentationUrl", "iconUrl"):
        value = card.get(key)
        if isinstance(value, str) and value:
            host = urlsplit(value).hostname or ""
            if not host_on_vendor(host, vendor_domain):
                flag(RejectReason.FOREIGN_URL, key, f"host {host!r}")

    description = card.get("description")
    if isinstance(description, str) and len(description) > MAX_DESCRIPTION:
        soft.append(LintFinding(None, "description", f"{len(description)} > {MAX_DESCRIPTION}"))
    for i, skill in enumerate(card.get("skills") or []):
        if not isinstance(skill, dict):
            continue
        sd = skill.get("description")
        if isinstance(sd, str) and len(sd) > MAX_SKILL_DESCRIPTION:
            soft.append(
                LintFinding(
                    None, f"skills[{i}].description", f"{len(sd)} > {MAX_SKILL_DESCRIPTION}"
                )
            )
        examples = [e for e in skill.get("examples") or [] if isinstance(e, str)]
        if len(examples) > MAX_EXAMPLES:
            soft.append(
                LintFinding(None, f"skills[{i}].examples", f"{len(examples)} > {MAX_EXAMPLES}")
            )
        for j, e in enumerate(examples):
            if len(e) > MAX_EXAMPLE_CHARS:
                soft.append(
                    LintFinding(
                        None, f"skills[{i}].examples[{j}]", f"{len(e)} > {MAX_EXAMPLE_CHARS}"
                    )
                )
    return LintResult(tuple(hard), tuple(soft))
