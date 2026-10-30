"""Phase 1 adversarial run: A1, A1b, A2 (x3), A3, A9 (x2) plus 3 honest controls.

Runs entirely in memory (InMemoryRepository, keys generated per run). Used by
`bourse attacks phase1` and by the unit tests.
"""

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jwcrypto import jwk

from bourse.contracts.registry import RegisterRequest, RejectReason, VendorCreate, VendorKeys
from bourse.crypto.card_sign import sign_card
from bourse.crypto.jcs import card_from_dict, card_to_dict
from bourse.crypto.keys import generate_key, jwks, thumbprint_kid
from bourse.registry.repo_memory import InMemoryRepository
from bourse.registry.service import RegistryService

DEFAULT_FIXTURES = Path("tests/fixtures/cards")


@dataclass(frozen=True)
class AttackResult:
    attack: str
    description: str
    expected: RejectReason | None  # None = honest control, must be accepted
    actual: RejectReason | None

    @property
    def is_control(self) -> bool:
        return self.expected is None

    @property
    def caught(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True)
class _Party:
    domain: str
    key: jwk.JWK

    @property
    def kid(self) -> str:
        return thumbprint_kid(self.key)

    @property
    def jwks_url(self) -> str:
        return f"https://{self.domain}/.well-known/jwks.json"


def _sign(
    card: dict[str, Any], party: _Party, kid: str | None = None, jku: str | None = None
) -> dict[str, Any]:
    signed = sign_card(card_from_dict(card), party.key, kid or party.kid, jku)
    return card_to_dict(signed)


async def run_phase1(fixtures_dir: Path = DEFAULT_FIXTURES) -> list[AttackResult]:
    def load(name: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((fixtures_dir / name).read_text())
        return data

    vendor_a = _Party("vendor-a.local", generate_key())
    vendor_b = _Party("vendor-b.local", generate_key())
    attacker = _Party("attacker.local", generate_key())
    service = RegistryService(InMemoryRepository())
    for party, name in ((vendor_a, "Vendor A"), (vendor_b, "Vendor B"), (attacker, "Attacker")):
        await service.add_vendor(
            VendorCreate(
                domain=party.domain,
                name=name,
                keys=VendorKeys(jwks_url=party.jwks_url, jwks=jwks([party.key])),
            )
        )

    results: list[AttackResult] = []

    async def submit(
        attack: str,
        description: str,
        expected: RejectReason | None,
        vendor: str,
        card: dict[str, Any],
    ) -> None:
        outcome = await service.register(RegisterRequest(vendor_domain=vendor, card=card))
        results.append(AttackResult(attack, description, expected, outcome.reason))

    async def setup(card: dict[str, Any], vendor: str) -> RejectReason | None:
        outcome = await service.register(RegisterRequest(vendor_domain=vendor, card=card))
        return outcome.reason

    steps: list[Callable[[], Awaitable[None]]] = []

    h1 = _sign(load("honest-a-profiler.json"), vendor_a)

    async def controls() -> None:
        await submit("H1", "honest card, vendor A", None, vendor_a.domain, h1)
        h2 = _sign(load("honest-a-stats.json"), vendor_a)
        await submit("H2", "honest card, vendor A", None, vendor_a.domain, h2)
        h3 = _sign(load("honest-b-pii.json"), vendor_b)
        await submit("H3", "honest card, vendor B", None, vendor_b.domain, h3)

    async def a1() -> None:
        forged = _sign(load("a1-forged-a-profiler.json"), attacker)
        await submit(
            "A1",
            "claims vendor A, attacker key",
            RejectReason.FORGED_SIGNATURE,
            vendor_a.domain,
            forged,
        )
        impersonating = _sign(load("a1b-impersonate-a.json"), attacker, jku=attacker.jwks_url)
        await submit(
            "A1b",
            "attacker-signed, interface URL on vendor A domain",
            RejectReason.DOMAIN_MISMATCH,
            attacker.domain,
            impersonating,
        )

    async def a2() -> None:
        edits: list[tuple[str, str, Callable[[dict[str, Any]], None]]] = [
            ("A2-desc", "signed card, description edited", lambda c: c.update(description="x")),
            (
                "A2-skill",
                "signed card, skill id edited",
                lambda c: c["skills"][0].update(id="stats.compute"),
            ),
            (
                "A2-url",
                "signed card, interface URL edited",
                lambda c: c["supportedInterfaces"][0].update(url="https://vendor-a.local/evil"),
            ),
        ]
        for name, description, edit in edits:
            tampered = copy.deepcopy(h1)
            edit(tampered)
            await submit(name, description, RejectReason.TAMPERED, vendor_a.domain, tampered)

    async def a3() -> None:
        base = load("honest-a-profiler.json")
        base["version"] = "1.0.2"
        swapped = _sign(base, attacker, kid=vendor_a.kid, jku=attacker.jwks_url)
        await submit(
            "A3",
            "vendor A kid, jku -> attacker JWKS",
            RejectReason.UNTRUSTED_JKU,
            vendor_a.domain,
            swapped,
        )

    async def a9() -> None:
        base = load("a9-a-units.json")
        setup_reason = await setup(_sign(base, vendor_a), vendor_a.domain)
        older = dict(base, version="1.1.0")
        reused = dict(base, description="Normalizes units, silently changed.")
        if setup_reason is not None:
            for name, expected in (
                ("A9-rollback", RejectReason.VERSION_ROLLBACK),
                ("A9-reuse", RejectReason.VERSION_REUSE),
            ):
                results.append(AttackResult(name, "setup 1.2.0 rejected", expected, setup_reason))
            return
        await submit(
            "A9-rollback",
            "1.2.0 registered, then 1.1.0",
            RejectReason.VERSION_ROLLBACK,
            vendor_a.domain,
            _sign(older, vendor_a),
        )
        await submit(
            "A9-reuse",
            "1.2.0 re-published with changed content",
            RejectReason.VERSION_REUSE,
            vendor_a.domain,
            _sign(reused, vendor_a),
        )

    steps.extend([controls, a1, a2, a3, a9])
    for step in steps:
        await step()
    return results


def format_table(results: list[AttackResult]) -> str:
    rows = [("attack", "expected", "actual", "ok", "description")]
    for r in results:
        rows.append(
            (
                r.attack,
                r.expected.value if r.expected else "ACCEPTED",
                r.actual.value if r.actual else "ACCEPTED",
                "yes" if r.caught else "NO",
                r.description,
            )
        )
    w = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = [
        f"{a:<{w[0]}}  {exp:<{w[1]}}  {act:<{w[2]}}  {ok:<{w[3]}}  {desc}"
        for a, exp, act, ok, desc in rows
    ]
    attacks = [r for r in results if not r.is_control]
    controls = [r for r in results if r.is_control]
    caught = sum(r.caught for r in attacks)
    false_rejects = sum(not r.caught for r in controls)
    lines.append("")
    lines.append(f"attacks caught: {caught}/{len(attacks)}")
    lines.append(f"honest false rejects: {false_rejects}/{len(controls)}")
    return "\n".join(lines)
