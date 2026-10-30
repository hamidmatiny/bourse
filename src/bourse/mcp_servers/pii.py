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

    return server
