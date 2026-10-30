import copy
from typing import Any

import httpx
import pytest

from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.registry import RegisterRequest, RejectReason, VerifyResult
from bourse.contracts.trust import Severity, TrustState
from bourse.crypto.card_sign import b64url_encode
from bourse.registry.attacks import AttackResult, format_table, run_phase1
from bourse.registry.service import RegistryPolicy, RegistryService
from bourse.registry.state import transition
from tests.registry_helpers import FIXTURES, Party, load_card, make_party, make_service

VA = "vendor-a.local"
CARD_URL = f"https://{VA}/.well-known/agent-card.json"
PROFILER_ID = "vendor-a-schema-profiler@vendor-a.local"


async def _register(service: RegistryService, vendor: str, card: dict[str, Any]) -> Any:
    return await service.register(RegisterRequest(vendor_domain=vendor, card=card))


async def test_phase1_attack_suite_all_caught() -> None:
    results = await run_phase1(FIXTURES)
    table = format_table(results)
    attacks = [r for r in results if not r.is_control]
    controls = [r for r in results if r.is_control]
    assert [r.attack for r in attacks] == [
        "A1",
        "A1b",
        "A2-desc",
        "A2-skill",
        "A2-url",
        "A3",
        "A9-rollback",
        "A9-reuse",
    ]
    assert len(controls) == 3
    assert all(r.caught for r in results), table
    assert "attacks caught: 8/8" in table
    assert "honest false rejects: 0/3" in table


async def test_honest_registration_starts_pending_with_revision() -> None:
    a = make_party(VA)
    service = await make_service(a)
    outcome = await _register(service, VA, a.sign(load_card("honest-a-profiler.json")))
    assert outcome.accepted and outcome.reason is None
    assert outcome.agent_id == PROFILER_ID
    assert outcome.state is TrustState.PENDING
    assert outcome.verify_result is VerifyResult.OK
    revisions = await service.repo.list_revisions(PROFILER_ID)
    assert [r.version for r in revisions] == ["1.0.0"]
    assert revisions[0].card["signatures"]


async def test_same_version_same_content_is_idempotent() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    await _register(service, VA, a.sign(card))
    again = await _register(service, VA, a.sign(card))  # new signature, same content
    assert again.accepted and again.unchanged
    assert len(await service.repo.list_revisions(PROFILER_ID)) == 1


async def test_upgrade_adds_revision_and_keeps_state() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    await _register(service, VA, a.sign(card))
    await transition(service.repo, PROFILER_ID, TrustState.ACTIVE, actor="t", reason="exam")
    upgraded = await _register(service, VA, a.sign(dict(card, version="1.1.0")))
    assert upgraded.accepted and upgraded.state is TrustState.ACTIVE
    assert [r.version for r in await service.repo.list_revisions(PROFILER_ID)] == [
        "1.0.0",
        "1.1.0",
    ]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda c: c["skills"][0].update(id="web.browse"), RejectReason.UNKNOWN_SKILL),
        (lambda c: c.update(skills=[]), RejectReason.UNKNOWN_SKILL),
        (lambda c: c.update(version="1.0"), RejectReason.MALFORMED_CARD),
        (lambda c: c.update(supportedInterfaces=[]), RejectReason.MALFORMED_CARD),
        (
            lambda c: c["provider"].update(url="https://attacker.local"),
            RejectReason.DOMAIN_MISMATCH,
        ),
        (
            lambda c: c["supportedInterfaces"].append(
                {"url": "https://vendor-a.local.attacker.local/x", "protocolBinding": "JSONRPC"}
            ),
            RejectReason.DOMAIN_MISMATCH,
        ),
    ],
    ids=[
        "unknown-skill",
        "no-skills",
        "not-semver",
        "no-interfaces",
        "provider-url",
        "suffix-host",
    ],
)
async def test_signed_but_invalid_cards(mutate: Any, reason: RejectReason) -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    mutate(card)
    outcome = await _register(service, VA, a.sign(card))
    assert outcome.reason is reason
    assert outcome.violation is not None


async def test_unsigned_unknown_vendor_and_malformed() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    assert (await _register(service, VA, card)).reason is RejectReason.UNSIGNED
    assert (await _register(service, "nobody.local", card)).reason is RejectReason.UNKNOWN_VENDOR
    bad = dict(card, notAField=1)
    assert (await _register(service, VA, bad)).reason is RejectReason.MALFORMED_CARD


async def test_inline_card_too_large() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = a.sign(load_card("honest-a-profiler.json"))
    card["description"] = "x" * (64 * 1024)
    assert (await _register(service, VA, card)).reason is RejectReason.CARD_TOO_LARGE


async def test_unsupported_alg_maps_to_reject_reason() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = a.sign(load_card("honest-a-profiler.json"))
    card["signatures"][0]["protected"] = b64url_encode(b'{"alg":"none","kid":"k"}')
    outcome = await _register(service, VA, card)
    assert outcome.reason is RejectReason.UNSUPPORTED_ALG
    assert outcome.violation is not None and outcome.violation.severity is Severity.HARD


async def test_pushed_hard_violation_does_not_quarantine() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    await _register(service, VA, a.sign(card))
    await transition(service.repo, PROFILER_ID, TrustState.ACTIVE, actor="t", reason="exam")
    tampered = a.sign(card)
    tampered["description"] = "changed"
    outcome = await _register(service, VA, tampered)
    assert outcome.reason is RejectReason.TAMPERED
    assert outcome.state is TrustState.ACTIVE
    violations = await service.repo.list_violations(PROFILER_ID)
    assert [v.kind for v in violations] == ["TAMPERED"]
    assert "source=pushed" in violations[0].evidence


class _CardServer:
    """MockTransport that serves one mutable card at the vendor's well-known URL."""

    def __init__(self) -> None:
        self.card: dict[str, Any] = {}
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return httpx.Response(200, json=self.card)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def _fetched_service(a: Party, server: _CardServer) -> RegistryService:
    service = await make_service(a, client=server.client())
    server.card = a.sign(load_card("honest-a-profiler.json"))
    outcome = await service.register(RegisterRequest(vendor_domain=VA, card_url=CARD_URL))
    assert outcome.accepted, outcome
    await transition(service.repo, PROFILER_ID, TrustState.ACTIVE, actor="t", reason="exam")
    return service


async def test_refresh_rollback_quarantines_active_agent() -> None:
    a = make_party(VA)
    server = _CardServer()
    service = await _fetched_service(a, server)
    server.card = a.sign(dict(load_card("honest-a-profiler.json"), version="0.9.0"))
    outcome = await service.refresh(PROFILER_ID)
    assert outcome.reason is RejectReason.VERSION_ROLLBACK
    assert outcome.state is TrustState.QUARANTINED
    audit = await service.repo.list_transitions(PROFILER_ID)
    assert [(t.from_state, t.to_state, t.actor) for t in audit] == [
        (TrustState.PENDING, TrustState.ACTIVE, "t"),
        (TrustState.ACTIVE, TrustState.QUARANTINED, "registry"),
    ]
    approved = ApprovalDecision(
        approved=True, by="operator", at=audit[-1].at, reason="vendor fixed card"
    )
    back = await transition(
        service.repo, PROFILER_ID, TrustState.ACTIVE, actor="admin", reason="ok", approval=approved
    )
    assert back.to_state is TrustState.ACTIVE
    assert server.requests == [CARD_URL, CARD_URL]


async def test_refresh_upgrade_accepted() -> None:
    a = make_party(VA)
    server = _CardServer()
    service = await _fetched_service(a, server)
    server.card = a.sign(dict(load_card("honest-a-profiler.json"), version="1.0.1"))
    outcome = await service.refresh(PROFILER_ID)
    assert outcome.accepted and outcome.version == "1.0.1"
    assert outcome.state is TrustState.ACTIVE


async def test_refresh_soft_failure_keeps_state() -> None:
    a = make_party(VA)
    server = _CardServer()
    service = await _fetched_service(a, server)
    server.card = load_card("honest-a-profiler.json")  # unsigned -> SOFT
    outcome = await service.refresh(PROFILER_ID)
    assert outcome.reason is RejectReason.UNSIGNED
    assert outcome.state is TrustState.ACTIVE


async def test_off_domain_card_url_never_fetched() -> None:
    a = make_party(VA)
    server = _CardServer()
    service = await make_service(a, client=server.client())
    for url in (
        "https://attacker.local/.well-known/agent-card.json",
        "http://169.254.169.254/.well-known/agent-card.json",
        "https://vendor-a.local.attacker.local/.well-known/agent-card.json",
    ):
        req = RegisterRequest(vendor_domain=VA, card_url=url)
        assert (await service.register(req)).reason is RejectReason.DOMAIN_MISMATCH
    assert server.requests == []


async def test_dev_hosts_allowlist() -> None:
    a = make_party(VA)
    server = _CardServer()
    service = await make_service(
        a, client=server.client(), policy=RegistryPolicy(dev_hosts=frozenset({"localhost"}))
    )
    card = load_card("honest-a-profiler.json")
    card["supportedInterfaces"][0]["url"] = "http://localhost:9001/a2a"
    server.card = a.sign(card)
    url = "http://localhost:9001/.well-known/agent-card.json"
    outcome = await service.register(RegisterRequest(vendor_domain=VA, card_url=url))
    assert outcome.accepted, outcome


async def test_revoked_agent_rejects_updates() -> None:
    a = make_party(VA)
    service = await make_service(a)
    card = load_card("honest-a-profiler.json")
    await _register(service, VA, a.sign(card))
    await transition(service.repo, PROFILER_ID, TrustState.REVOKED, actor="t", reason="gone")
    outcome = await _register(service, VA, a.sign(dict(card, version="2.0.0")))
    assert outcome.reason is RejectReason.AGENT_REVOKED


async def test_refresh_unknown_agent_and_no_url() -> None:
    a = make_party(VA)
    service = await make_service(a)
    assert (await service.refresh("nope@x")).reason is RejectReason.UNKNOWN_AGENT
    await _register(service, VA, a.sign(load_card("honest-a-profiler.json")))
    assert (await service.refresh(PROFILER_ID)).reason is RejectReason.FETCH_FAILED


async def test_tags_and_skills_recorded() -> None:
    a = make_party(VA)
    service = await make_service(a)
    await _register(service, VA, a.sign(copy.deepcopy(load_card("honest-a-profiler.json"))))
    agent = await service.repo.get_agent(PROFILER_ID)
    assert agent is not None
    assert [s.value for s in agent.skills] == ["schema.profile"]
    assert agent.tags == ("dataset", "profile", "schema")


def test_attack_table_columns_separated_by_two_spaces() -> None:
    results = [
        AttackResult("H1", "honest", None, None),
        AttackResult("A1", "forged", RejectReason.FORGED_SIGNATURE, RejectReason.TAMPERED),
    ]
    lines = format_table(results).splitlines()
    assert lines[0].split() == ["attack", "expected", "actual", "ok", "description"]
    assert lines[1] == "H1      ACCEPTED          ACCEPTED  yes  honest"
    assert lines[2] == "A1      FORGED_SIGNATURE  TAMPERED  NO   forged"
    assert lines[-2:] == ["attacks caught: 0/1", "honest false rejects: 0/1"]
