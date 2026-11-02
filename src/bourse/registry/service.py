"""Registry logic (ADR-0013). Pure: all I/O goes through Repository and an injected httpx client.

Check order for a card: parse -> revoked? -> signature (pinned JWKS) -> skills -> domains ->
semver -> version history. Signature comes first so an edited card is always TAMPERED, never a
later reason.

Attribution: a violation found on a card the registry fetched itself from the vendor's host
(refresh, or register with card_url) is attributed to the vendor. Only attributed HARD
violations move an ACTIVE agent to QUARANTINED. Pushed inline cards can be submitted by anyone,
so their violations are recorded but do not change the agent's state.
"""

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.types import AgentCard

from bourse.contracts.registry import (
    AgentRecord,
    CardRevision,
    RegisterRequest,
    RegistrationOutcome,
    RejectReason,
    Vendor,
    VendorCreate,
    VerifyResult,
)
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.crypto.card_sign import verify_card
from bourse.crypto.jcs import CanonicalizationError, card_from_dict, card_to_dict, payload_digest
from bourse.registry.fetch import MAX_CARD_BYTES, CardTooLarge, FetchFailed, fetch_card
from bourse.registry.lint import LintConfig, host_on_vendor, lint_card
from bourse.registry.repo import Repository, RevisionExists
from bourse.registry.semver import SemVer
from bourse.registry.state import transition

VERIFY_TO_REJECT: dict[VerifyResult, RejectReason] = {
    VerifyResult.NO_SIGNATURE: RejectReason.UNSIGNED,
    VerifyResult.BAD_SIGNATURE: RejectReason.TAMPERED,
    VerifyResult.UNKNOWN_KID: RejectReason.FORGED_SIGNATURE,
    VerifyResult.UNTRUSTED_JKU: RejectReason.UNTRUSTED_JKU,
    VerifyResult.UNSUPPORTED_ALG: RejectReason.UNSUPPORTED_ALG,
    VerifyResult.MALFORMED: RejectReason.MALFORMED_CARD,
}

HARD_REASONS: frozenset[RejectReason] = frozenset(
    {
        RejectReason.FORGED_SIGNATURE,
        RejectReason.TAMPERED,
        RejectReason.UNTRUSTED_JKU,
        RejectReason.UNSUPPORTED_ALG,
        RejectReason.DOMAIN_MISMATCH,
        RejectReason.VERSION_ROLLBACK,
        RejectReason.VERSION_REUSE,
        RejectReason.LINT_INJECTION,
        RejectReason.HIDDEN_UNICODE,
        RejectReason.FOREIGN_URL,
        RejectReason.MARKUP,
    }
)
LINT_WARNING = "LINT_WARNING"

_SKILL_IDS = frozenset(s.value for s in SkillId)
_SLUG = re.compile(r"[^a-z0-9]+")


def make_agent_id(name: str, vendor_domain: str) -> str:
    slug = _SLUG.sub("-", name.lower()).strip("-")
    if not slug:
        raise ValueError("card name has no usable characters")
    return f"{slug}@{vendor_domain}"


@dataclass(frozen=True)
class RegistryPolicy:
    dev_hosts: frozenset[str] = field(default_factory=frozenset)
    max_card_bytes: int = MAX_CARD_BYTES
    lint: LintConfig = field(default_factory=LintConfig)

    def host_allowed(self, url: str, vendor_domain: str) -> bool:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            return False
        if not host:
            return False
        host = host.lower()
        # ADR-0034: the vendor domain or any subdomain of it (vendor hostnames in compose).
        return host_on_vendor(host, vendor_domain) or host in self.dev_hosts


class _Reject(Exception):
    def __init__(self, reason: RejectReason, evidence: str) -> None:
        super().__init__(f"{reason.value}: {evidence}")
        self.reason = reason
        self.evidence = evidence


def _default_clock() -> datetime:
    return datetime.now(UTC)


class RegistryService:
    def __init__(
        self,
        repo: Repository,
        fetch_client: httpx.AsyncClient | None = None,
        policy: RegistryPolicy | None = None,
        clock: Callable[[], datetime] = _default_clock,
        on_accepted: Callable[[str], object] | None = None,
    ) -> None:
        self.repo = repo
        # Called with agent_id after an accepted registration that leaves the agent PENDING
        # (the admission exam hook, ADR-0037).
        self.on_accepted = on_accepted
        self._client = fetch_client
        self._policy = policy or RegistryPolicy()
        self._clock = clock

    async def add_vendor(self, req: VendorCreate) -> Vendor:
        vendor = Vendor(domain=req.domain, name=req.name, keys=req.keys, created_at=self._clock())
        await self.repo.add_vendor(vendor)
        return vendor

    async def register(self, req: RegisterRequest) -> RegistrationOutcome:
        vendor = await self.repo.get_vendor(req.vendor_domain)
        if vendor is None:
            return RegistrationOutcome(accepted=False, reason=RejectReason.UNKNOWN_VENDOR)
        if req.card_url is not None:
            if not self._policy.host_allowed(req.card_url, vendor.domain):
                # Checked before any request: the registry never fetches off-vendor URLs.
                return RegistrationOutcome(accepted=False, reason=RejectReason.DOMAIN_MISMATCH)
            try:
                data = await self._fetch(req.card_url)
            except CardTooLarge:
                return RegistrationOutcome(accepted=False, reason=RejectReason.CARD_TOO_LARGE)
            except FetchFailed:
                return RegistrationOutcome(accepted=False, reason=RejectReason.FETCH_FAILED)
            return await self._ingest(vendor, data, card_url=req.card_url, attributable=True)
        assert req.card is not None
        if len(json.dumps(req.card).encode()) > self._policy.max_card_bytes:
            return RegistrationOutcome(accepted=False, reason=RejectReason.CARD_TOO_LARGE)
        return await self._ingest(vendor, req.card, card_url=None, attributable=False)

    async def refresh(self, agent_id: str) -> RegistrationOutcome:
        agent = await self.repo.get_agent(agent_id)
        if agent is None:
            return RegistrationOutcome(accepted=False, reason=RejectReason.UNKNOWN_AGENT)
        vendor = await self.repo.get_vendor(agent.vendor_domain)
        if vendor is None:
            return RegistrationOutcome(accepted=False, reason=RejectReason.UNKNOWN_VENDOR)
        if agent.card_url is None:
            return await self._reject(
                RejectReason.FETCH_FAILED, "no card_url on record", agent, agent_id, None, False
            )
        try:
            data = await self._fetch(agent.card_url)
        except CardTooLarge as exc:
            return await self._reject(
                RejectReason.CARD_TOO_LARGE, str(exc), agent, agent_id, None, True
            )
        except FetchFailed as exc:
            return await self._reject(
                RejectReason.FETCH_FAILED, str(exc), agent, agent_id, None, True
            )
        return await self._ingest(
            vendor, data, card_url=agent.card_url, attributable=True, expected_agent_id=agent_id
        )

    async def _fetch(self, url: str) -> dict[str, Any]:
        if self._client is None:
            raise FetchFailed("registry has no HTTP client configured")
        return await fetch_card(self._client, url, max_bytes=self._policy.max_card_bytes)

    async def _ingest(
        self,
        vendor: Vendor,
        data: dict[str, Any],
        *,
        card_url: str | None,
        attributable: bool,
        expected_agent_id: str | None = None,
    ) -> RegistrationOutcome:
        try:
            card = card_from_dict(data)
            agent_id = make_agent_id(card.name, vendor.domain)
        except ValueError:
            return RegistrationOutcome(accepted=False, reason=RejectReason.MALFORMED_CARD)
        existing = await self.repo.get_agent(agent_id)
        if expected_agent_id is not None and agent_id != expected_agent_id:
            prior = await self.repo.get_agent(expected_agent_id)
            return await self._reject(
                RejectReason.MALFORMED_CARD,
                f"card at {card_url} now names agent {agent_id}",
                prior,
                expected_agent_id,
                None,
                attributable,
            )
        if existing is not None and existing.state is TrustState.REVOKED:
            return await self._reject(
                RejectReason.AGENT_REVOKED, "agent is revoked", existing, agent_id, None, False
            )

        verify_result = verify_card(card, vendor.keys)
        try:
            if verify_result is not VerifyResult.OK:
                raise _Reject(VERIFY_TO_REJECT[verify_result], f"verify_result={verify_result}")
            skills = self._check_skills(card)
            self._check_domains(card, vendor)
            lint = lint_card(card_to_dict(card), vendor.domain, self._policy.lint)
            if lint.hard:
                first = lint.hard[0]
                assert first.reason is not None
                details = "; ".join(f"{f.field}: {f.detail}" for f in lint.hard[:5])
                raise _Reject(first.reason, f"{len(lint.hard)} lint finding(s): {details}")
            new_version = self._parse_version(card.version)
            digest = payload_digest(card)
            if existing is not None:
                current = SemVer(existing.current_version)
                if new_version < current:
                    raise _Reject(
                        RejectReason.VERSION_ROLLBACK,
                        f"{card.version} < current {existing.current_version}",
                    )
                if new_version == current:
                    if digest == existing.current_digest:
                        return RegistrationOutcome(
                            accepted=True,
                            agent_id=agent_id,
                            version=card.version,
                            verify_result=verify_result,
                            state=existing.state,
                            unchanged=True,
                        )
                    raise _Reject(
                        RejectReason.VERSION_REUSE,
                        f"version {card.version} re-published with digest {digest[:16]}",
                    )
        except _Reject as rej:
            return await self._reject(
                rej.reason, rej.evidence, existing, agent_id, verify_result, attributable
            )
        except CanonicalizationError:
            return RegistrationOutcome(accepted=False, reason=RejectReason.MALFORMED_CARD)

        now = self._clock()
        tags = tuple(sorted({t for s in card.skills for t in s.tags}))
        if card_url is None and existing is not None:
            card_url = existing.card_url
        record = AgentRecord(
            agent_id=agent_id,
            vendor_domain=vendor.domain,
            name=card.name,
            card_url=card_url,
            state=existing.state if existing else TrustState.PENDING,
            current_version=card.version,
            current_digest=digest,
            skills=skills,
            tags=tags,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        revision = CardRevision(
            agent_id=agent_id,
            version=card.version,
            digest_sha256=digest,
            verify_result=verify_result,
            received_at=now,
            card=card_to_dict(card),
        )
        try:
            await self.repo.save_registration(record, revision)
        except RevisionExists:
            return await self._reject(
                RejectReason.VERSION_REUSE,
                f"revision {card.version} already stored",
                existing,
                agent_id,
                verify_result,
                attributable,
            )
        for warning in lint.soft:
            await self.repo.add_violation(
                Violation(
                    kind=LINT_WARNING,
                    severity=Severity.SOFT,
                    agent_id=agent_id,
                    evidence=f"{warning.field}: {warning.detail}",
                    at=now,
                )
            )
        if self.on_accepted is not None and record.state is TrustState.PENDING:
            self.on_accepted(agent_id)
        return RegistrationOutcome(
            accepted=True,
            agent_id=agent_id,
            version=card.version,
            verify_result=verify_result,
            state=record.state,
        )

    @staticmethod
    def _check_skills(card: AgentCard) -> tuple[SkillId, ...]:
        ids = [s.id for s in card.skills]
        if not ids:
            raise _Reject(RejectReason.UNKNOWN_SKILL, "card declares no skills")
        unknown = sorted(set(ids) - _SKILL_IDS)
        if unknown:
            raise _Reject(RejectReason.UNKNOWN_SKILL, f"{len(unknown)} skill id(s) not in taxonomy")
        return tuple(dict.fromkeys(SkillId(i) for i in ids))

    def _check_domains(self, card: AgentCard, vendor: Vendor) -> None:
        urls: Iterable[str] = [i.url for i in card.supported_interfaces]
        if not card.supported_interfaces:
            raise _Reject(RejectReason.MALFORMED_CARD, "card declares no supportedInterfaces")
        if card.HasField("provider") and card.provider.url:
            urls = [*urls, card.provider.url]
        bad = [u for u in urls if not self._policy.host_allowed(u, vendor.domain)]
        if bad:
            raise _Reject(
                RejectReason.DOMAIN_MISMATCH, f"{len(bad)} URL host(s) not on {vendor.domain}"
            )

    @staticmethod
    def _parse_version(raw: str) -> SemVer:
        try:
            return SemVer(raw)
        except ValueError as exc:
            raise _Reject(RejectReason.MALFORMED_CARD, "version is not semver") from exc

    async def _reject(
        self,
        reason: RejectReason,
        evidence: str,
        existing: AgentRecord | None,
        agent_id: str,
        verify_result: VerifyResult | None,
        attributable: bool,
    ) -> RegistrationOutcome:
        now = self._clock()
        severity = Severity.HARD if reason in HARD_REASONS else Severity.SOFT
        source = "fetched" if attributable else "pushed"
        violation = Violation(
            kind=reason.value,
            severity=severity,
            agent_id=agent_id,
            evidence=f"{evidence}; source={source}",
            at=now,
        )
        await self.repo.add_violation(violation)
        state = existing.state if existing else None
        if (
            existing is not None
            and severity is Severity.HARD
            and attributable
            and existing.state is TrustState.ACTIVE
        ):
            record = await transition(
                self.repo,
                agent_id,
                TrustState.QUARANTINED,
                actor="registry",
                reason=f"HARD violation {reason.value}",
                now=now,
            )
            state = record.to_state
        return RegistrationOutcome(
            accepted=False,
            reason=reason,
            agent_id=agent_id,
            verify_result=verify_result,
            state=state,
            violation=violation,
        )
