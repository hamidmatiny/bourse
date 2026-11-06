"""Seeded BOURSE-Bench case generator (ADR-0051). Deterministic: same seed -> same bytes.

Every value comes from `random.Random(seed)`. Profile, stats and unit truth come from our
deterministic table/unit tools (as the exam bank). PII truth is the set of cells the generator
planted, by construction; it is never derived from the regex the agents use, so cases the
regex cannot see (obfuscated emails, international phones, PII inside free text) count against
every agent alike.
"""

import csv
import io
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, get_args

from bourse.contracts.bench import BenchCase, Domain, Split
from bourse.contracts.skills_io import PiiKind
from bourse.mcp_servers.table import column_stats_impl, profile_columns_impl
from bourse.mcp_servers.units import parse_header_unit_impl
from bourse.trust.honeytokens import luhn_check_digit

GENERATOR_VERSION = "1"
DOMAINS: tuple[Domain, ...] = get_args(Domain)
SPLIT_NAMESPACE: dict[Split, str] = {"dev": "bench-dev:", "test": "bench-test:"}
REQUEST_TEXT = "audit this dataset for PII, units and summary stats"

FIRST = [
    "Ava", "Liam", "Noah", "Mia", "Zoe", "Omar", "Lena", "Ravi", "Sara", "Theo", "Ines", "Kai",
    "Priya", "Wei", "Sofia", "Diego", "Amara", "Jonas", "Yuki", "Fatima",
]  # fmt: skip
LAST = [
    "Nguyen", "Okafor", "Silva", "Kowalski", "Tremblay", "Haddad", "Larsen", "Moreau", "Ito",
    "Chen", "Cote", "Mensah", "Novak", "Reyes", "Schmidt", "Petrov",
]  # fmt: skip
# Hard-PII: real given names / surnames that are also common English words.
WORD_FIRST = ["Rose", "Will", "Amber", "Hunter", "Grant", "Summer", "Bill", "Faith", "Mark", "Iris"]
WORD_LAST = ["Hunter", "Grant", "Stone", "Field", "Brook", "Fisher", "Baker", "Young", "King"]
# Decoy: a color column whose values are also first names.
COLORS = ["Rose", "Amber", "Olive", "Hazel", "Ruby", "Violet", "Jade", "Ivory", "Coral"]
SITES = ["north", "south", "east", "west", "central"]
NOTES = ["leave at door", "fragile", "", "gift wrap", "call on arrival", "back entrance"]
MAIL_HOSTS = ["example.com", "example.ca", "example.org", "mail.example.net"]
INTL_PHONES = [
    "+44 20 7946 {a:04d}",
    "+33 1 42 68 {b:02d} {c:02d}",
    "+49 30 901820{d:02d}",
    "+81 3-5555-{a:04d}",
    "+61 2 9374 {a:04d}",
]


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)
    pii: set[tuple[int, str, PiiKind]] = field(default_factory=set)

    def add(self, values: dict[str, str], pii: dict[str, PiiKind] | None = None) -> None:
        row = len(self.rows)
        self.rows.append([values[h] for h in self.header])
        for column, kind in (pii or {}).items():
            if values[column]:
                self.pii.add((row, column, kind))


class Gen:
    def __init__(self, seed: str) -> None:
        self.rng = random.Random(seed)

    def pick(self, items: list[str]) -> str:
        return self.rng.choice(items)

    def name(self) -> tuple[str, str]:
        return self.pick(FIRST), self.pick(LAST)

    def email(self, first: str, last: str) -> str:
        return f"{first.lower()}.{last.lower()}{self.rng.randrange(100)}@{self.pick(MAIL_HOSTS)}"

    def phone(self) -> str:
        a, b, c = (
            self.rng.randrange(200, 990),
            self.rng.randrange(200, 999),
            self.rng.randrange(10_000),
        )
        style = self.rng.randrange(3)
        if style == 0:
            return f"({a}) {b}-{c:04d}"
        if style == 1:
            return f"{a}-{b}-{c:04d}"
        return f"+1 {a} {b} {c:04d}"

    def card(self, valid: bool) -> str:
        body = self.pick(["4", "5"]) + "".join(str(self.rng.randrange(10)) for _ in range(14))
        check = luhn_check_digit(body)
        if not valid:
            check = str((int(check) + 1 + self.rng.randrange(9)) % 10)
        digits = body + check
        sep = self.pick([" ", "-"])
        return sep.join(digits[i : i + 4] for i in range(0, 16, 4))

    def sin(self, valid: bool) -> str:
        body = str(self.rng.randrange(1, 8)) + "".join(
            str(self.rng.randrange(10)) for _ in range(7)
        )
        check = luhn_check_digit(body)
        if not valid:
            check = str((int(check) + 1) % 10)
        d = body + check
        return f"{d[:3]} {d[3:6]} {d[6:]}"

    def day(self, start: date, span: int) -> str:
        return (start + timedelta(days=self.rng.randrange(span))).isoformat()

    def num(self, lo: float, hi: float, digits: int) -> str:
        return f"{self.rng.uniform(lo, hi):.{digits}f}"

    def maybe(self, value: str, p_null: float) -> str:
        return "" if self.rng.random() < p_null else value


def _people(g: Gen, n: int) -> Table:
    t = Table(["id", "name", "email", "phone", "card", "sin", "signup_date", "active", "age"])
    for i in range(n):
        first, last = g.name()
        card_ok, sin_ok = g.rng.random() < 0.7, g.rng.random() < 0.6
        pii: dict[str, PiiKind] = {"name": "PERSON_NAME", "email": "EMAIL", "phone": "PHONE"}
        if card_ok:
            pii["card"] = "CARD"
        if sin_ok:
            pii["sin"] = "SIN"
        t.add(
            {
                "id": str(1 + i),
                "name": f"{first} {last}",
                "email": g.email(first, last),
                "phone": g.phone(),
                "card": g.card(card_ok),
                "sin": g.sin(sin_ok),
                "signup_date": g.day(date(2024, 1, 1), 500),
                "active": g.pick(["true", "false"]),
                "age": g.maybe(str(g.rng.randrange(18, 81)), 0.1),
            },
            pii,
        )
    return t


def _orders(g: Gen, n: int) -> Table:
    t = Table(["order_id", "customer", "amount", "ordered_on", "contact", "notes", "paid", "vol_L"])
    for i in range(n):
        first, last = g.name()
        by_mail = g.rng.random() < 0.5
        t.add(
            {
                "order_id": str(5000 + i),
                "customer": f"{first} {last}",
                "amount": g.num(5, 900, 2),
                "ordered_on": g.day(date(2025, 1, 1), 300),
                "contact": g.email(first, last) if by_mail else g.phone(),
                "notes": g.pick(NOTES),
                "paid": g.pick(["true", "false"]),
                "vol_L": g.maybe(g.num(0.5, 30, 2), 0.1),
            },
            {"customer": "PERSON_NAME", "contact": "EMAIL" if by_mail else "PHONE"},
        )
    return t


def _measurements(g: Gen, n: int) -> Table:
    t = Table(["sample_id", "weight_lb", "temp_degF", "dist_km", "dur_min", "site"])
    for i in range(n):
        t.add(
            {
                "sample_id": str(100 + i),
                "weight_lb": g.num(90, 260, 1),
                "temp_degF": g.num(-10, 105, 1),
                "dist_km": g.maybe(g.num(0.5, 42, 2), 0.1),
                "dur_min": str(g.rng.randrange(5, 300)),
                "site": g.pick(SITES),
            }
        )
    return t


def _clinic(g: Gen, n: int) -> Table:
    t = Table(
        ["visit_id", "patient", "dob", "phone", "temp_degF", "height_in", "weight_kg", "dur_min",
         "clinician"]
    )  # fmt: skip
    clinicians = [" ".join(g.name()) for _ in range(3)]
    for i in range(n):
        first, last = g.name()
        t.add(
            {
                "visit_id": str(70000 + i),
                "patient": f"{first} {last}",
                "dob": g.day(date(1940, 1, 1), 25000),
                "phone": g.maybe(g.phone(), 0.15),
                "temp_degF": g.num(96.5, 103.5, 1),
                "height_in": g.num(55, 78, 1),
                "weight_kg": g.num(40, 130, 1),
                "dur_min": str(g.rng.randrange(10, 90)),
                "clinician": g.pick(clinicians),
            },
            {"patient": "PERSON_NAME", "phone": "PHONE", "clinician": "PERSON_NAME"},
        )
    return t


def _shipments(g: Gen, n: int) -> Table:
    t = Table(
        ["shipment_id", "sender", "sender_email", "recipient", "recipient_phone", "weight_oz",
         "dist_mi", "vol_gal", "shipped_on"]
    )  # fmt: skip
    for i in range(n):
        sf, sl = g.name()
        rf, rl = g.name()
        t.add(
            {
                "shipment_id": f"SH{900000 + i}",
                "sender": f"{sf} {sl}",
                "sender_email": g.email(sf, sl),
                "recipient": f"{rf} {rl}",
                "recipient_phone": g.phone(),
                "weight_oz": g.num(1, 400, 1),
                "dist_mi": g.num(1, 2800, 1),
                "vol_gal": g.maybe(g.num(0.1, 60, 2), 0.1),
                "shipped_on": g.day(date(2025, 3, 1), 200),
            },
            {
                "sender": "PERSON_NAME",
                "sender_email": "EMAIL",
                "recipient": "PERSON_NAME",
                "recipient_phone": "PHONE",
            },
        )
    return t


def _obfuscated_email(g: Gen, first: str, last: str) -> str:
    host, tld = g.pick(["mail", "post", "inbox"]), g.pick(["com", "ca", "org"])
    style = g.rng.randrange(3)
    if style == 0:
        return f"{first.lower()} [at] {host} [dot] {tld}"
    if style == 1:
        return f"{first.lower()}.{last.lower()} at {host} dot {tld}"
    return f"{first.lower()}(at){host}(dot){tld}"


def _intl_phone(g: Gen) -> str:
    return g.pick(INTL_PHONES).format(
        a=g.rng.randrange(10_000),
        b=g.rng.randrange(100),
        c=g.rng.randrange(100),
        d=g.rng.randrange(100),
    )


def _sku(g: Gen) -> str:
    """Decoy: a product code in NANP phone shape (the regex flags it; it is not PII)."""
    return f"{g.rng.randrange(200, 990)}-{g.rng.randrange(200, 999)}-{g.rng.randrange(10_000):04d}"


def _bad_sin(g: Gen) -> str:
    """Decoy: 9-digit reference that fails the SIN check (never PII)."""
    return g.sin(valid=False).replace(" ", "")


HARD_FEATURES = ("split_name", "word_name", "obfuscated", "intl_phone", "free_text", "decoys")


def _hard_pii(g: Gen, n: int) -> Table:
    """Hard-PII case: 3-5 hard features plus a numeric column (so stats/report have truth)."""
    k = g.rng.randrange(3, len(HARD_FEATURES))
    features = sorted(g.rng.sample(HARD_FEATURES, k), key=HARD_FEATURES.index)
    header = ["rec"]
    if "split_name" in features:
        header += ["first", "last"]
    if "word_name" in features:
        header += ["contact_ref"]
    if "obfuscated" in features:
        header += ["reach"]
    if "intl_phone" in features:
        header += ["tel"]
    if "free_text" in features:
        header += ["notes"]
    if "decoys" in features:
        header += ["color", "sku", "ref_no", "batch"]
    header += ["email", "amount", "weight_kg"]
    t = Table(header)
    for i in range(n):
        first, last = g.name()
        values: dict[str, str] = {"rec": str(300 + i)}
        pii: dict[str, PiiKind] = {}
        if "split_name" in features:
            values |= {"first": first, "last": last}
            pii |= {"first": "PERSON_NAME", "last": "PERSON_NAME"}
        if "word_name" in features:
            values["contact_ref"] = f"{g.pick(WORD_FIRST)} {g.pick(WORD_LAST)}"
            pii["contact_ref"] = "PERSON_NAME"
        if "obfuscated" in features:
            values["reach"] = _obfuscated_email(g, first, last)
            pii["reach"] = "EMAIL"
        if "intl_phone" in features:
            values["tel"] = _intl_phone(g)
            pii["tel"] = "PHONE"
        if "free_text" in features:
            embed = g.rng.random() < 0.5
            values["notes"] = (
                f"call {g.phone()} after 5pm" if embed else g.pick([*NOTES[:2], "no contact"])
            )
            if embed:
                pii["notes"] = "PHONE"
        if "decoys" in features:
            values |= {
                "color": g.pick(COLORS),
                "sku": _sku(g),
                "ref_no": g.card(valid=False),
                "batch": _bad_sin(g),
            }
        values |= {
            "email": g.email(first, last),
            "amount": g.num(1, 500, 2),
            "weight_kg": g.maybe(g.num(0.2, 80, 2), 0.1),
        }
        pii["email"] = "EMAIL"
        t.add(values, pii)
    return t


BUILDERS: dict[Domain, Callable[[Gen, int], Table]] = {
    "people": _people,
    "orders": _orders,
    "measurements": _measurements,
    "clinic": _clinic,
    "shipments": _shipments,
    "hard_pii": _hard_pii,
}


def _csv(table: Table) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(table.header)
    writer.writerows(table.rows)
    return buf.getvalue()


def _truth(csv_text: str, table: Table) -> dict[str, Any]:
    conversions = [
        conv.model_dump(mode="json")
        for h in table.header
        if (conv := parse_header_unit_impl(h).conversion) is not None
    ]
    pos = {h: i for i, h in enumerate(table.header)}
    findings = [
        {"row": r, "column": c, "kind": k}
        for r, c, k in sorted(table.pii, key=lambda f: (f[0], pos[f[1]], f[2]))
    ]
    return {
        "profile": profile_columns_impl(csv_text).model_dump(mode="json"),
        "stats": column_stats_impl(csv_text).model_dump(mode="json"),
        "pii": {"findings": findings},
        "units": {"conversions": conversions},
    }


def case_seed(split: Split, base_seed: str, index: int) -> str:
    return f"{SPLIT_NAMESPACE[split]}{base_seed}:{index}"


def domain_for(index: int) -> Domain:
    return DOMAINS[index % len(DOMAINS)]


def case_id(split: Split, index: int, domain: Domain) -> str:
    return f"{split}-{index:04d}-{domain}"


def generate_case(split: Split, base_seed: str, index: int) -> BenchCase:
    domain = domain_for(index)
    seed = case_seed(split, base_seed, index)
    return generate(split, case_id(split, index, domain), domain, seed)


def generate(split: Split, cid: str, domain: Domain, seed: str) -> BenchCase:
    if not seed.startswith(SPLIT_NAMESPACE[split]):
        raise ValueError(f"seed {seed!r} is not in the {split} namespace")
    g = Gen(seed)
    table = BUILDERS[domain](g, g.rng.randrange(10, 31))
    text = _csv(table)
    return BenchCase(
        case_id=cid,
        split=split,
        domain=domain,
        seed=seed,
        request_text=REQUEST_TEXT,
        csv_text=text,
        truth=_truth(text, table),
    )
