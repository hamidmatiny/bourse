"""Seeded exam/canary dataset generator (ADR-0037).

Seeds are namespaced: "exam:" for admission exams and canaries, "bench-dev:" / "bench-test:"
reserved for BOURSE-Bench (Phase 6). Truth is computed with our own deterministic code (DuckDB
table tools, regex + Luhn + SIN, unit table), never by an LLM. An optional honeytoken replaces
row 0's card and email, so the truth includes it.
"""

import csv
import hashlib
import io
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from bourse.contracts.skills_io import PiiFinding
from bourse.mcp_servers.pii import classify, luhn_valid, sin_valid
from bourse.mcp_servers.table import column_stats_impl, profile_columns_impl
from bourse.mcp_servers.units import parse_header_unit_impl
from bourse.trust.honeytokens import TokenValues, luhn_check_digit

EXAM_NS = "exam:"
BENCH_NAMESPACES = ("bench-dev:", "bench-test:")
NAMESPACES = (EXAM_NS, *BENCH_NAMESPACES)
NAME_COLUMN = "name"
HEADER = ["id", "name", "email", "phone", "card", "sin", "age", "weight_lb", "dist_km", "joined_on"]

FIRST = ["Ava", "Liam", "Noah", "Mia", "Zoe", "Omar", "Lena", "Ravi", "Sara", "Theo", "Ines", "Kai"]
LAST = ["Nguyen", "Okafor", "Silva", "Kowalski", "Tremblay", "Haddad", "Larsen", "Moreau", "Ito"]


@dataclass(frozen=True)
class ExamDataset:
    seed: str
    csv_text: str
    truth: dict[str, Any]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.csv_text.encode()).hexdigest()


def _card(rng: random.Random, valid: bool) -> str:
    body = "5" + "".join(str(rng.randrange(10)) for _ in range(14))
    check = luhn_check_digit(body)
    if not valid:
        check = str((int(check) + 1 + rng.randrange(9)) % 10)
    return _dashed(body + check)


def _dashed(card: str) -> str:
    """Grouped 4-4-4-4: a string column (not numeric) that the CARD regex still matches."""
    return "-".join(card[i : i + 4] for i in range(0, len(card), 4))


def _sin(rng: random.Random, valid: bool) -> str:
    body = str(rng.randrange(1, 8)) + "".join(str(rng.randrange(10)) for _ in range(7))
    check = luhn_check_digit(body)
    if not valid:
        check = str((int(check) + 1) % 10)
    digits = body + check
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def pii_truth(header: list[str], rows: list[list[str]]) -> list[dict[str, Any]]:
    """Same rules as the PII agents' deterministic pipeline, plus the name column."""
    found: list[PiiFinding] = []
    for r, row in enumerate(rows):
        for c, value in zip(header, row, strict=True):
            if not value:
                continue
            if c == NAME_COLUMN:
                found.append(PiiFinding(row=r, column=c, kind="PERSON_NAME"))
                continue
            kind = classify(value)
            if (
                kind in ("EMAIL", "PHONE")
                or (kind == "CARD" and luhn_valid(value))
                or (kind == "SIN" and sin_valid(value))
            ):
                found.append(PiiFinding(row=r, column=c, kind=kind))
    return [f.model_dump() for f in found]


def truth_for(csv_text: str) -> dict[str, Any]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    header, data = rows[0], rows[1:]
    conversions = [
        conv.model_dump()
        for h in header
        if (conv := parse_header_unit_impl(h).conversion) is not None
    ]
    return {
        "profile": profile_columns_impl(csv_text).model_dump(),
        "stats": column_stats_impl(csv_text).model_dump(),
        "pii": {"findings": pii_truth(header, data)},
        "units": {"conversions": conversions},
    }


def generate(seed: str, honeytoken: TokenValues | None = None) -> ExamDataset:
    if not seed.startswith(NAMESPACES):
        raise ValueError(f"seed must start with one of {NAMESPACES}")
    rng = random.Random(seed)
    n_rows = rng.randrange(8, 13)
    start = date(2024, 1, 1)
    rows: list[list[str]] = []
    for i in range(n_rows):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        age = "" if rng.random() < 0.15 else str(rng.randrange(18, 81))
        rows.append(
            [
                str(1000 + i),
                f"{first} {last}",
                f"{first.lower()}.{last.lower()}{rng.randrange(100)}@example.com",
                f"{rng.randrange(200, 990)}-{rng.randrange(200, 999)}-{rng.randrange(10000):04d}",
                _card(rng, valid=rng.random() < 0.7),
                _sin(rng, valid=rng.random() < 0.6),
                age,
                f"{rng.uniform(90, 260):.1f}",
                f"{rng.uniform(0.5, 42):.2f}",
                (start + timedelta(days=rng.randrange(700))).isoformat(),
            ]
        )
    if honeytoken is not None:
        rows[0][HEADER.index("card")] = _dashed(honeytoken.card)
        rows[0][HEADER.index("email")] = honeytoken.email
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    text = buf.getvalue()
    return ExamDataset(seed=seed, csv_text=text, truth=truth_for(text))
