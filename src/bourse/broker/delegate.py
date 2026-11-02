"""A2A delegation (ADR-0026).

Per attempt: fetch the agent's live card; its JCS digest must equal the registry's current
revision digest, otherwise no message is sent, a SOFT CARD_DRIFT violation is reported, a
registry refresh is triggered and the next candidate is tried. The message carries one
DataPart (the SKILL_IO input) and metadata {skill_id, task_id, deadline_s}; the artifact must
validate as the SKILL_IO output. At most MAX_ATTEMPTS attempts per subtask, one per ranked
candidate. Every attempt is one append-only delegation row.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from bourse.broker.repo import RunRepository
from bourse.broker.spotcheck import spot_check
from bourse.contracts.broker import (
    DelegationRecord,
    DelegationState,
    SelectionDecision,
    SubtaskResult,
    SubtaskStatus,
)
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import SKILL_IO, DatasetRef, ReportIn
from bourse.contracts.tasks import Subtask
from bourse.contracts.trust import Severity, TrustState, ViolationReport
from bourse.crypto.jcs import card_from_dict, payload_digest
from bourse.ledger.recorder import LedgerRecorder, agent_major
from bourse.registry.fetch import FetchFailed, fetch_card
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.deadline import DeadlineExceeded, deadline
from bourse.stack.smoke import send_task
from bourse.telemetry import tracer

MAX_ATTEMPTS = 2
# The agent enforces deadline_s itself; the broker's wall-clock deadline adds this margin for
# card fetch, connection setup and the final status event.
DEADLINE_GRACE_S = 15.0
CARD_DRIFT = "CARD_DRIFT"
BREAKER_DETAIL = "BreakerOpen"

_A2A_STATES: dict[str, DelegationState] = {
    "TASK_STATE_FAILED": DelegationState.FAILED,
    "TASK_STATE_REJECTED": DelegationState.REJECTED,
    "TASK_STATE_INPUT_REQUIRED": DelegationState.INTERRUPTED,
    "TASK_STATE_AUTH_REQUIRED": DelegationState.INTERRUPTED,
}


class RunAborted(Exception):
    """Breaker tripped (here or reported by an agent): no further delegations in this run."""


class RegistryClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, admin_token: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._admin = {"Authorization": f"Bearer {admin_token}"}

    async def active_agents(self, skill: SkillId) -> list[AgentRecord]:
        resp = await self._http.get(
            f"{self._base}/agents",
            params={"skill": skill.value, "state": TrustState.ACTIVE.value, "limit": 200},
        )
        resp.raise_for_status()
        return [AgentRecord.model_validate(a) for a in resp.json()]

    async def report_violation(self, agent_id: str, report: ViolationReport) -> None:
        resp = await self._http.post(
            f"{self._base}/agents/{agent_id}/violations",
            json=report.model_dump(mode="json"),
            headers=self._admin,
        )
        resp.raise_for_status()

    async def refresh(self, agent_id: str) -> int:
        resp = await self._http.post(f"{self._base}/agents/{agent_id}/refresh")
        return resp.status_code


def build_input(
    skill: SkillId, dataset: DatasetRef, upstream: Mapping[SkillId, dict[str, Any]]
) -> BaseModel:
    """SKILL_IO input from the dataset and validated outputs of the subtask's ancestors."""
    if skill is SkillId.REPORT_COMPOSE:
        parts: dict[str, Any] = {}
        for key, source in (
            ("profile", SkillId.SCHEMA_PROFILE),
            ("stats", SkillId.STATS_COMPUTE),
            ("pii", SkillId.PII_DETECT),
            ("units", SkillId.UNITS_NORMALIZE),
        ):
            if source in upstream:
                parts[key] = SKILL_IO[source][1].model_validate(upstream[source])
        return ReportIn(dataset=dataset, **parts)
    return SKILL_IO[skill][0].model_validate({"dataset": dataset.model_dump()})


class Delegator:
    def __init__(
        self,
        registry: RegistryClient,
        http: httpx.AsyncClient,
        runs: RunRepository,
        breaker: CircuitBreaker,
        *,
        deadline_s: float,
        max_attempts: int = MAX_ATTEMPTS,
        grace_s: float = DEADLINE_GRACE_S,
        heartbeat: Callable[[], None] = lambda: None,
        ledger: LedgerRecorder | None = None,
    ) -> None:
        self.registry = registry
        self.http = http
        self.runs = runs
        self.breaker = breaker
        self.deadline_s = deadline_s
        self.max_attempts = max_attempts
        self.grace_s = grace_s
        self.ledger = ledger
        self.heartbeat = heartbeat

    async def run_subtask(
        self,
        run_id: str,
        subtask: Subtask,
        decision: SelectionDecision,
        agents: Mapping[str, AgentRecord],
        payload: BaseModel,
    ) -> SubtaskResult:
        ranked: Sequence[str] = (
            [decision.chosen, *decision.fallbacks] if decision.chosen is not None else []
        )
        # Resume-safe (ADR-0027): rows from an earlier run of this level keep attempt numbers
        # unique; earlier failed attempts count toward the limit and exclude their agents. A
        # completed attempt whose output was lost before the checkpoint does not count.
        earlier = [
            d for d in await self.runs.list_delegations(run_id) if d.subtask_id == subtask.id
        ]
        failed = [d for d in earlier if d.state is not DelegationState.COMPLETED]
        tried = {d.agent_id for d in failed}
        limit = max(0, self.max_attempts - len(failed))
        remaining = [a for a in ranked if a not in tried][:limit]
        last_kind = (failed[-1].error_kind or "attempts_exhausted") if failed else "no_candidates"
        attempt = len(earlier)
        for agent_id in remaining:
            if self.breaker.tripped:
                raise RunAborted(self.breaker.reason or "breaker open")
            attempt += 1
            agent = agents[agent_id]
            with tracer().start_as_current_span("delegation attempt") as span:
                span.set_attribute("run_id", run_id)
                span.set_attribute("subtask_id", subtask.id)
                span.set_attribute("skill", subtask.skill.value)
                span.set_attribute("agent_id", agent_id)
                span.set_attribute("attempt", attempt)
                record, output, detail, spot = await self.attempt(
                    run_id, subtask, agent, attempt, payload
                )
                await self.runs.add_delegation(record)
                span.set_attribute("outcome", record.state.value)
                if detail:
                    span.set_attribute("detail", detail[:200])
                if self.ledger is not None:
                    row = await self.ledger.record(
                        record, major=agent_major(agent.current_version), spot_check_passed=spot
                    )
                    span.set_attribute("tokens_in", row.tokens_in)
                    span.set_attribute("tokens_out", row.tokens_out)
                    span.set_attribute("shadow_usd", row.shadow_usd)
            self.heartbeat()
            if record.state is DelegationState.COMPLETED:
                return SubtaskResult(
                    subtask_id=subtask.id,
                    skill=subtask.skill,
                    status=SubtaskStatus.COMPLETED,
                    agent_id=agent_id,
                    attempts=attempt,
                    output=output,
                )
            last_kind = record.error_kind or record.state.value.lower()
            if record.error_kind == "llm_unavailable" and BREAKER_DETAIL in detail:
                self.breaker.trip(f"{agent_id} reported {BREAKER_DETAIL} on {subtask.id}")
                raise RunAborted(self.breaker.reason or "breaker open")
        return SubtaskResult(
            subtask_id=subtask.id,
            skill=subtask.skill,
            status=SubtaskStatus.FAILED,
            agent_id=None,
            attempts=attempt,
            error_kind=last_kind,
        )

    async def attempt(
        self,
        run_id: str,
        subtask: Subtask,
        agent: AgentRecord,
        attempt: int,
        payload: BaseModel,
    ) -> tuple[DelegationRecord, dict[str, Any] | None, str, bool | None]:
        """Returns (row, output, detail, spot_check_passed); spot is None unless a
        contract-valid output was checked."""
        started = time.monotonic()

        def record(state: DelegationState, kind: str | None) -> DelegationRecord:
            return DelegationRecord(
                run_id=run_id,
                subtask_id=subtask.id,
                skill=subtask.skill,
                agent_id=agent.agent_id,
                revision_digest=agent.current_digest,
                attempt=attempt,
                state=state,
                error_kind=kind,
                wall_ms=(time.monotonic() - started) * 1000,
                at=datetime.now(UTC),
            )

        if agent.card_url is None:
            return record(DelegationState.ERROR, "no_card_url"), None, "", None
        try:
            card = card_from_dict(await fetch_card(self.http, agent.card_url))
            live_digest = payload_digest(card)
        except (FetchFailed, ValueError):
            return record(DelegationState.ERROR, "card_fetch_failed"), None, "", None
        if live_digest != agent.current_digest:
            await self._report_drift(agent, live_digest)
            return record(DelegationState.CARD_DRIFT, "card_drift"), None, "", None

        task_id = f"{run_id}:{subtask.id}:{attempt}"
        try:
            async with deadline(self.deadline_s + self.grace_s):
                result = await send_task(
                    card,
                    subtask.skill.value,
                    payload.model_dump(mode="json"),
                    task_id,
                    self.deadline_s,
                    self.http,
                )
        except DeadlineExceeded:
            return record(DelegationState.TIMEOUT, "deadline"), None, "", None
        except Exception as exc:  # transport and A2A client errors end this attempt only
            return record(DelegationState.ERROR, type(exc).__name__), None, "", None

        if result.state == "TASK_STATE_COMPLETED":
            if result.output is None:
                return record(DelegationState.FAILED_CONTRACT, "no_artifact"), None, "", None
            try:
                output = SKILL_IO[subtask.skill][1].model_validate(result.output)
            except ValidationError:
                return record(DelegationState.FAILED_CONTRACT, "invalid_output"), None, "", None
            check = spot_check(subtask.skill, payload, output, task_id)
            if not check.passed:
                failed = record(DelegationState.FAILED_SPOTCHECK, "spotcheck")
                return failed, None, check.detail, False
            return record(DelegationState.COMPLETED, None), output.model_dump(mode="json"), "", True
        state = _A2A_STATES.get(result.state)
        if state is None:
            kind = "stream_ended_" + result.state.removeprefix("TASK_STATE_").lower()
            return record(DelegationState.ERROR, kind), None, result.error, None
        kind = result.error_kind or (
            "interrupted" if state is DelegationState.INTERRUPTED else state.value.lower()
        )
        return record(state, kind), None, result.error, None

    async def _report_drift(self, agent: AgentRecord, live_digest: str) -> None:
        evidence = f"live card digest {live_digest} != registry digest {agent.current_digest}"
        # Best effort: a registry error must not stop the run from moving to the next agent.
        try:
            await self.registry.report_violation(
                agent.agent_id,
                ViolationReport(kind=CARD_DRIFT, severity=Severity.SOFT, evidence=evidence),
            )
            await self.registry.refresh(agent.agent_id)
        except httpx.HTTPError:
            return
