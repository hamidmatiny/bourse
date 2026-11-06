"""Deterministic PII candidate tools: regex_scan, luhn_check, sin_check (ADR-0016). No LLM.

regex_scan matches each value as a whole (after trimming), so one value yields at most one
candidate kind: EMAIL, PHONE (NANP, 10 digits, optional +1), CARD (13-19 digits with optional
single space/dash separators), SIN (9 digits as 3-3-3 or plain). Validity is separate:
luhn_check for cards; sin_check = 9 digits, Luhn-valid, first digit not 0 or 8.
"""

import re
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from bourse.mcp_servers.common import check_size, run_limited

CandidateKind = Literal["EMAIL", "PHONE", "CARD", "SIN"]

_PATTERNS: list[tuple[CandidateKind, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("CARD", re.compile(r"\d(?:[ -]?\d){12,18}")),
    ("PHONE", re.compile(r"(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}")),
    ("SIN", re.compile(r"\d{3}[ -]?\d{3}[ -]?\d{3}")),
]


class PiiCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    kind: CandidateKind


class RegexScanOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[PiiCandidate]


class ChecksOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: list[bool]


def classify(value: str) -> CandidateKind | None:
    text = value.strip()
    digits = sum(ch.isdigit() for ch in text)
    for kind, pattern in _PATTERNS:
        if not pattern.fullmatch(text):
            continue
        # A 10-digit card-shaped string is a phone, not a card; CARD needs >= 13 digits.
        if kind == "CARD" and digits < 13:
            continue
        return kind
    return None


def _digits(number: str) -> str | None:
    stripped = re.sub(r"[ -]", "", number.strip())
    return stripped if stripped.isdigit() else None


def luhn_valid(number: str) -> bool:
    digits = _digits(number)
    if digits is None or len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def sin_valid(number: str) -> bool:
    digits = _digits(number)
    if digits is None or len(digits) != 9 or digits[0] in "08":
        return False
    return luhn_valid(digits)


# contact_check (ADR-0052): deterministic shape check for findings an LLM proposed. It never
# finds anything by itself; it only accepts or rejects (value, kind) pairs.
_EMAIL = _PATTERNS[0][1]
_AT = re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+", re.IGNORECASE)
_DOT = re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+", re.IGNORECASE)
# NANP anywhere in the text, or an international number: "+", 1-3 digit country code, then
# digit groups with single space/dot/dash separators; 8-15 digits in total (E.164 max 15).
_NANP_IN_TEXT = re.compile(r"(?<!\d)" + _PATTERNS[2][1].pattern + r"(?!\d)")
_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,5}\)?){1,6}(?!\d)")
ContactKind = Literal["EMAIL", "PHONE", "CARD", "SIN"]


def deobfuscate_email(value: str) -> str:
    return _DOT.sub(".", _AT.sub("@", value.strip()))


def contact_valid(value: str, kind: ContactKind) -> bool:
    text = value.strip()
    if kind == "EMAIL":
        return _EMAIL.fullmatch(deobfuscate_email(text)) is not None
    if kind == "PHONE":
        if _NANP_IN_TEXT.search(text):
            return True
        return any(8 <= sum(c.isdigit() for c in m.group(0)) <= 15 for m in _INTL.finditer(text))
    if kind == "CARD":
        return classify(text) == "CARD" and luhn_valid(text)
    return sin_valid(text)


def contact_check_impl(values: list[str], kinds: list[ContactKind]) -> ChecksOut:
    check_size(*values)
    if len(values) != len(kinds):
        raise ValueError("values and kinds must have the same length")
    return ChecksOut(results=[contact_valid(v, k) for v, k in zip(values, kinds, strict=True)])


def regex_scan_impl(values: list[str]) -> RegexScanOut:
    check_size(*values)
    found = [
        PiiCandidate(index=i, kind=kind)
        for i, v in enumerate(values)
        if (kind := classify(v)) is not None
    ]
    return RegexScanOut(candidates=found)


def luhn_check_impl(numbers: list[str]) -> ChecksOut:
    check_size(*numbers)
    return ChecksOut(results=[luhn_valid(n) for n in numbers])


def sin_check_impl(numbers: list[str]) -> ChecksOut:
    check_size(*numbers)
    return ChecksOut(results=[sin_valid(n) for n in numbers])


def build_server() -> MCPServer[Any]:
    server = MCPServer("bourse-pii", version="0.1.0")

    @server.tool()
    async def regex_scan(values: list[str]) -> RegexScanOut:
        """Classify each value as an EMAIL, PHONE, CARD or SIN candidate (whole-value match)."""
        return await run_limited(lambda: regex_scan_impl(values))

    @server.tool()
    async def luhn_check(numbers: list[str]) -> ChecksOut:
        """Luhn checksum per number; spaces and dashes ignored."""
        return await run_limited(lambda: luhn_check_impl(numbers))

    @server.tool()
    async def sin_check(numbers: list[str]) -> ChecksOut:
        """Canadian SIN validity: 9 digits, Luhn-valid, first digit not 0 or 8."""
        return await run_limited(lambda: sin_check_impl(numbers))

    @server.tool()
    async def contact_check(values: list[str], kinds: list[ContactKind]) -> ChecksOut:
        """Accept/reject proposed (value, kind) PII pairs: EMAIL after [at]/[dot] de-obfuscation,
        PHONE as NANP or +country number anywhere in the text, CARD Luhn, SIN check."""
        return await run_limited(lambda: contact_check_impl(values, kinds))

    return server
