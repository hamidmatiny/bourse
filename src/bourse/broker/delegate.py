"""A2A delegation (ADR-0026).

Per attempt: fetch the agent's live card; its JCS digest must equal the registry's current
revision digest, otherwise no message is sent, a SOFT CARD_DRIFT violation is reported, a
registry refresh is triggered and the next candidate is tried. The message carries one
DataPart (the SKILL_IO input) and metadata {skill_id, task_id, deadline_s}; the artifact must
validate as the SKILL_IO output. At most MAX_ATTEMPTS attempts per subtask, one per ranked
candidate. Every attempt is one append-only delegation row.
"""

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from bourse.broker.inflight import InFlight, InFlightHandle
from bourse.broker.interrupts import SESSION_SMUGGLING, evaluate_interrupt
from bourse.broker.repo import RunRepository
from bourse.broker.spotcheck import spot_check
from bourse.contracts.broker import (
    DelegationRecord,
    DelegationState,
    Delivery,
    SelectionDecision,
    SubtaskResult,
    SubtaskStatus,
)
from bourse.contracts.metering import TaskBudget, TaskBudgetStatus
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import SKILL_IO, DatasetRef, ReportIn
from bourse.contracts.tasks import Subtask
from bourse.contracts.trust import Severity, TrustState, ViolationReport
from bourse.crypto.jcs import card_from_dict, payload_digest
from bourse.ledger.recorder import LedgerRecorder
from bourse.registry.fetch import FetchFailed, fetch_card
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.deadline import DeadlineExceeded, deadline
from bourse.stack.smoke import send_task
from bourse.telemetry import tracer
from bourse.trust.reliability_epoch import effective_agent_major

MAX_ATTEMPTS = 2
# The agent enforces deadline_s itself; the broker's wall-clock deadline adds this margin for
# card fetch, connection setup and the final status event.
DEADLINE_GRACE_S = 15.0
CARD_DRIFT = "CARD_DRIFT"
SPOTCHECK_FAIL = "SPOTCHECK_FAIL"
USAGE_MISREPORT = "USAGE_MISREPORT"
BREAKER_DETAIL = "BreakerOpen"
MISREPORT_RATIO = 0.10

_A2A_STATES: dict[str, DelegationState] = {
    "TASK_STATE_FAILED": DelegationState.FAILED,
    "TASK_STATE_REJECTED": DelegationState.REJECTED,
    "TASK_STATE_INPUT_REQUIRED": DelegationState.INTERRUPTED,
    "TASK_STATE_AUTH_REQUIRED": DelegationState.INTERRUPTED,
}


def _as_int(value: Any) -> int | None:
    """Protobuf Struct JSON (MessageToDict) turns integers into floats."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


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

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        resp = await self._http.get(f"{self._base}/agents/{agent_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return AgentRecord.model_validate(resp.json())

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


class MeterBudgetClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, broker_key: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {broker_key}"}

    async def register(self, budget: TaskBudget) -> None:
        resp = await self._http.post(
            f"{self._base}/budgets", json=budget.model_dump(mode="json"), headers=self._auth
        )
        resp.raise_for_status()

    async def status(self, task_id: str) -> TaskBudgetStatus:
        """Meter evidence for one task (ADR-0049 amendment 2). Raises httpx.HTTPError."""
        resp = await self._http.get(
            f"{self._base}/budgets/status", params={"task_id": task_id}, headers=self._auth
        )
        resp.raise_for_status()
        return TaskBudgetStatus.model_validate(resp.json())


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
        inflight: InFlight | None = None,
        budgets: MeterBudgetClient | None = None,
        task_max_tokens: int = 8 * 1024,
        task_max_calls: int = 20,
        usage_lookup: Callable[[str], Awaitable[list[Any]]] | None = None,
        contention_queue_fraction: float = 0.5,
        spot_checks: bool = True,
        report_violations: bool = True,
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
        self.inflight = inflight or InFlight()
        self.budgets = budgets
        self.task_max_tokens = task_max_tokens
        self.task_max_calls = task_max_calls
        self._input_rounds: dict[str, int] = {}
        self._usage_lookup = usage_lookup
        self._contention_fraction = contention_queue_fraction
        # ADR-0051 baselines B1/B2 turn both off (no trust engine); B3 keeps both on.
        self.spot_checks = spot_checks
        self.report_violations = report_violations
        # Ordered log of A2A messages actually sent (tasks + canaries); phase5 exposure.
        self.deliveries: list[Delivery] = []

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
                        record,
                        major=effective_agent_major(agent.current_version, agent.tags),
                        spot_check_passed=spot,
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
        *,
        bourse_kind: str = "task",
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

        live = await self.registry.get_agent(agent.agent_id)
        if live is None or live.state is not TrustState.ACTIVE:
            return record(DelegationState.CANCELED_QUARANTINE, "quarantined"), None, "", None

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
        if self.budgets is not None:
            try:
                await self.budgets.register(
                    TaskBudget(
                        task_id=task_id,
                        max_tokens=self.task_max_tokens,
                        max_calls=self.task_max_calls,
                    )
                )
            except httpx.HTTPError:
                return record(DelegationState.ERROR, "budget_register_failed"), None, "", None

        handle: InFlightHandle | None = None

        def on_task(a2a_id: str) -> None:
            nonlocal handle
            handle = InFlightHandle(agent.agent_id, card, a2a_id, self.http)
            self.inflight.add(handle)

        self.deliveries.append(
            Delivery(
                agent_id=agent.agent_id,
                task_id=task_id,
                kind=(
                    "canary"
                    if bourse_kind == "canary"
                    else "exam"
                    if bourse_kind == "exam"
                    else "task"
                ),
                at=datetime.now(UTC),
            )
        )
        try:
            try:
                async with deadline(self.deadline_s + self.grace_s):
                    result = await send_task(
                        card,
                        subtask.skill.value,
                        payload.model_dump(mode="json"),
                        task_id,
                        self.deadline_s,
                        self.http,
                        on_task=on_task,
                        bourse_kind=bourse_kind,
                    )
            except DeadlineExceeded:
                if handle is not None:
                    await self.inflight.cancel_agent(agent.agent_id)
                return record(DelegationState.TIMEOUT, "deadline"), None, "", None
            except Exception as exc:  # transport and A2A client errors end this attempt only
                return record(DelegationState.ERROR, type(exc).__name__), None, "", None

            live = await self.registry.get_agent(agent.agent_id)
            if live is None or live.state is not TrustState.ACTIVE:
                if handle is not None:
                    await self.inflight.cancel_handle(handle)
                return record(DelegationState.CANCELED_QUARANTINE, "quarantined"), None, "", None

            if result.state == "TASK_STATE_INPUT_REQUIRED":
                round_index = self._input_rounds.get(task_id, 0)
                decision = evaluate_interrupt(
                    data_parts=result.data_parts,
                    text_parts=result.text_parts,
                    extra_parts=result.extra_parts,
                    payload=payload,
                    round_index=round_index,
                )
                self._input_rounds[task_id] = round_index + 1
                if decision.accepted:
                    return record(DelegationState.INTERRUPTED, "input_required"), None, "", None
                if handle is not None:
                    await self.inflight.cancel_handle(handle)
                await self._report(
                    agent.agent_id,
                    SESSION_SMUGGLING,
                    Severity.SOFT,
                    decision.detail or "INPUT_REQUIRED rejected",
                )
                return (
                    record(DelegationState.SESSION_POLICY, "session_smuggling"),
                    None,
                    result.error,
                    None,
                )

            if result.state == "TASK_STATE_COMPLETED":
                if result.output is None:
                    return record(DelegationState.FAILED_CONTRACT, "no_artifact"), None, "", None
                try:
                    output = SKILL_IO[subtask.skill][1].model_validate(result.output)
                except ValidationError:
                    return record(DelegationState.FAILED_CONTRACT, "invalid_output"), None, "", None
                await self._check_misreport(agent.agent_id, task_id, result.artifact_metadata)
                dumped = output.model_dump(mode="json")
                if not self.spot_checks:
                    if await self._queue_contention(task_id):
                        return (
                            record(DelegationState.CONTENTION, "queue_wait"),
                            dumped,
                            "queue_wait_ms > fraction of deadline",
                            None,
                        )
                    return (record(DelegationState.COMPLETED, None), dumped, "", None)
                check = spot_check(subtask.skill, payload, output, task_id)
                if not check.passed:
                    await self._report(
                        agent.agent_id,
                        SPOTCHECK_FAIL,
                        Severity.SOFT,
                        check.detail or "spot check",
                    )
                    failed = record(DelegationState.FAILED_SPOTCHECK, "spotcheck")
                    return failed, None, check.detail, False
                if await self._queue_contention(task_id):
                    return (
                        record(DelegationState.CONTENTION, "queue_wait"),
                        dumped,
                        "queue_wait_ms > fraction of deadline",
                        True,
                    )
                return (record(DelegationState.COMPLETED, None), dumped, "", True)
            state = _A2A_STATES.get(result.state)
            if state is None:
                kind = "stream_ended_" + result.state.removeprefix("TASK_STATE_").lower()
                return record(DelegationState.ERROR, kind), None, result.error, None
            kind = result.error_kind or (
                "interrupted" if state is DelegationState.INTERRUPTED else state.value.lower()
            )
            return record(state, kind), None, result.error, None
        finally:
            if handle is not None:
                self.inflight.remove(handle)

    async def _report(self, agent_id: str, kind: str, severity: Severity, evidence: str) -> None:
        if not self.report_violations:
            return
        try:
            await self.registry.report_violation(
                agent_id, ViolationReport(kind=kind, severity=severity, evidence=evidence)
            )
            live = await self.registry.get_agent(agent_id)
            if live is not None and live.state is TrustState.QUARANTINED:
                await self.inflight.cancel_agent(agent_id)
        except httpx.HTTPError:
            return

    async def _queue_contention(self, task_id: str) -> bool:
        if self._usage_lookup is None or self._contention_fraction <= 0:
            return False
        limit_ms = self.deadline_s * self._contention_fraction * 1000.0
        rows = await self._usage_lookup(task_id)
        return any(float(getattr(r, "queue_wait_ms", 0) or 0) > limit_ms for r in rows)

    async def _check_misreport(self, agent_id: str, task_id: str, metadata: dict[str, Any]) -> None:
        claimed_in = _as_int(metadata.get("tokens_in", metadata.get("tokensIn")))
        claimed_out = _as_int(metadata.get("tokens_out", metadata.get("tokensOut")))
        if claimed_in is None or claimed_out is None:
            return
        measured_in = 0
        measured_out = 0
        if self._usage_lookup is not None:
            rows = await self._usage_lookup(task_id)
            measured_in = sum(getattr(r, "tokens_in", 0) for r in rows)
            measured_out = sum(getattr(r, "tokens_out", 0) for r in rows)
        claimed = claimed_in + claimed_out
        measured = measured_in + measured_out
        if measured == 0:
            mismatch = claimed > 0
        else:
            mismatch = abs(claimed - measured) / measured > MISREPORT_RATIO
        if mismatch:
            await self._report(
                agent_id,
                USAGE_MISREPORT,
                Severity.SOFT,
                f"claimed {claimed} tokens vs measured {measured} on {task_id}",
            )

    async def _report_drift(self, agent: AgentRecord, live_digest: str) -> None:
        evidence = f"live card digest {live_digest} != registry digest {agent.current_digest}"
        # Best effort: a registry error must not stop the run from moving to the next agent.
        try:
            await self._report(agent.agent_id, CARD_DRIFT, Severity.SOFT, evidence)
            await self.registry.refresh(agent.agent_id)
        except httpx.HTTPError:
            return
