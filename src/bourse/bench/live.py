"""Live wiring of the bench harness to the compose stack (ADR-0051).

B1-B3 run through `broker_from_settings` with the baseline's policy and trust controls. B0 is
the generalist (`bench.solo`). Adversaries go through the ADR-0042 phase5 runner. PII separation
sends each case straight to one PII agent over A2A (no broker), with a registered meter budget.
All LLM calls go through the meter; cost comes from its `llm_usage` rows.
"""

import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from bourse.adversaries.pool import Pool
from bourse.agents.common.spec import METER_TASK_HEADER
from bourse.bench.harness import CellOutcome, CellRunner, SubtaskOutcome, adversary_key
from bourse.bench.solo import run_solo
from bourse.broker.delegate import MeterBudgetClient, RegistryClient
from bourse.broker.graph import Broker, new_run_id
from bourse.broker.runner import broker_from_settings, budget
from bourse.broker.select import MeasuredPolicy, PriorPolicy, RandomPolicy
from bourse.config import Settings
from bourse.contracts.bench import (
    AdversaryResult,
    AdversarySpec,
    BenchCase,
    ConfigSpec,
    PiiCaseResult,
    canonical_json,
)
from bourse.contracts.broker import BrokerControls, DelegationState
from bourse.contracts.metering import BudgetSpec, LlmUsageRecord, TaskBudget
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import DatasetRef, PiiIn
from bourse.contracts.tasks import IntakeRequest
from bourse.crypto.jcs import card_from_dict
from bourse.grading.graders import grade
from bourse.llm.omniroute import OpenAICompatProvider
from bourse.llm.router import Router
from bourse.llm.submit_tool import ChatClient
from bourse.meter.repo import MeterRepository
from bourse.meter.repo_sql import SqlMeterRepository
from bourse.registry.fetch import fetch_card
from bourse.runtime.breaker import GLOBAL_BREAKER
from bourse.runtime.budget import BudgetLedger
from bourse.stack.launcher import BROKER_METER_ID, admin_token, meter_key
from bourse.stack.smoke import send_task

POLICY = {"B1": "random", "B2": "prior", "B3": "measured"}
NO_TRUST = BrokerControls(spot_checks=False, report_violations=False, canaries=False)


def controls_for(spec: ConfigSpec) -> BrokerControls:
    if spec.baseline in ("B1", "B2"):
        return NO_TRUST
    if spec.ablation == "no-spotcheck":
        return BrokerControls(spot_checks=False)
    if spec.ablation == "no-canary":
        return BrokerControls(canaries=False)
    return BrokerControls()


def usage_cost(rows: Sequence[LlmUsageRecord]) -> tuple[int, int, float, int]:
    """(tokens_in, tokens_out, compute_ms, contention 429s) from meter rows."""
    compute = sum(
        r.total_duration_ms if r.total_duration_ms is not None else r.wall_ms for r in rows
    )
    contention = sum(r.status == 429 and (r.error_kind or "") == "contention" for r in rows)
    return (
        sum(r.tokens_in for r in rows),
        sum(r.tokens_out for r in rows),
        float(compute),
        contention,
    )


class BrokerCellRunner:
    """B1-B3: one broker run per cell. B1 reseeds its random policy per (case, repeat); B2 and
    B3 seed their tie-breaks per (case, repeat), hashed with the subtask (ADR-0051)."""

    def __init__(
        self,
        broker: Broker,
        budget_spec: BudgetSpec,
        spec: ConfigSpec,
        *,
        pool: Pool | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.broker = broker
        self.budget_spec = budget_spec
        self.spec = spec
        self.pool = pool  # mixed pool (6b): harm events and quarantines are collected per cell
        self.settings = settings

    async def run_cell(self, case: BenchCase, repeat: int) -> CellOutcome:
        broker = self.broker
        if self.spec.baseline == "B1":
            broker.policy = RandomPolicy(f"{case.seed}:{repeat}")
        elif self.spec.baseline == "B2":
            if not isinstance(broker.policy, PriorPolicy):
                raise TypeError(f"B2 needs PriorPolicy, got {type(broker.policy).__name__}")
            broker.policy = PriorPolicy(broker.policy.config, tie_seed=f"{case.seed}:{repeat}")
        elif self.spec.baseline == "B3" and isinstance(broker.policy, MeasuredPolicy):
            broker.policy.tie_seed = f"{case.seed}:{repeat}"  # reproducible per-case ties
        request = IntakeRequest(
            dataset=DatasetRef.from_csv(case.csv_text),
            request_text=case.request_text,
            budget=self.budget_spec,
        )
        started = time.monotonic()
        since = datetime.now(UTC)
        pool_fields: dict[str, Any] = {}
        if self.pool is not None:
            pool_fields = {
                "pool_epoch": self.pool.epoch,
                "pool_agents": tuple(sorted(self.pool.agent_ids)),
            }
        try:
            report = await broker.start(request)
        except Exception as exc:  # one cell fails; the bench goes on and records it
            return CellOutcome(
                run_id="-",
                status="ERROR",
                planner_used="-",
                subtasks=(),
                tokens_in=0,
                tokens_out=0,
                compute_ms=0.0,
                wall_s=time.monotonic() - started,
                contention_events=0,
                error=f"{type(exc).__name__}: {exc}"[:300],
                **pool_fields,
            )
        if self.pool is not None and self.settings is not None:
            from bourse.adversaries.pool import cell_harm_events, quarantined_members

            served = tuple(
                d.agent_id for d in report.delegations if d.agent_id in self.pool.agent_ids
            )
            lines, agents, attempts = await cell_harm_events(
                self.settings, self.pool, served, since
            )
            pool_fields |= {
                "delegations": len(report.delegations),
                "adversary_served": served,
                "harm_lines": tuple(lines),
                "harm_agents": tuple(agents),
                "attempt_agents": tuple(attempts),
                "quarantined_adversaries": tuple(
                    await quarantined_members(self.settings, self.pool)
                ),
            }
        rows = await broker.usage.list_usage(run_id=report.run_id)
        t_in, t_out, compute, c429 = usage_cost(rows)
        contention = c429 + sum(d.state is DelegationState.CONTENTION for d in report.delegations)
        return CellOutcome(
            run_id=report.run_id,
            status=report.status.value,
            planner_used=report.planner_used,
            subtasks=tuple(
                SubtaskOutcome(r.subtask_id, r.skill, r.status.value, r.agent_id, r.output)
                for r in report.subtasks
            ),
            tokens_in=t_in,
            tokens_out=t_out,
            compute_ms=compute,
            wall_s=report.wall_ms / 1000,
            contention_events=contention,
            error=report.abort_reason,
            **pool_fields,
        )


def meter_llm_factory(settings: Settings, http: httpx.AsyncClient) -> Callable[[str], ChatClient]:
    """B0's LLM: the roster model through the meter, tagged with the B0 task id."""
    key = meter_key(BROKER_METER_ID)

    def llm_for(task_id: str) -> ChatClient:
        provider = OpenAICompatProvider(
            settings.bourse_meter_url,
            settings.bourse_model_large,
            key,
            http,
            provider_name="meter",
            headers={METER_TASK_HEADER: task_id},
        )
        return Router([provider], BudgetLedger(budget(settings), run_id=task_id), GLOBAL_BREAKER)

    return llm_for


class SoloCellRunner:
    """B0: the generalist runs the template plan; cost from the meter's usage rows."""

    def __init__(
        self,
        llm_for: Callable[[str], ChatClient],
        usage: MeterRepository,
        deadline_s: float,
        max_tokens: int,
    ) -> None:
        self.llm_for = llm_for
        self.usage = usage
        self.deadline_s = deadline_s
        self.max_tokens = max_tokens

    async def run_cell(self, case: BenchCase, repeat: int) -> CellOutcome:
        run_id = "bench-b0-" + new_run_id().removeprefix("run-")
        started = time.monotonic()
        subs = await run_solo(
            self.llm_for,
            run_id,
            DatasetRef.from_csv(case.csv_text),
            max_tokens=self.max_tokens,
            deadline_s=self.deadline_s,
        )
        wall = time.monotonic() - started
        rows = await self.usage.list_usage(run_id=run_id)
        t_in, t_out, compute, c429 = usage_cost(rows)
        ok = all(s.status.value == "COMPLETED" for s in subs)
        return CellOutcome(
            run_id=run_id,
            status="SUCCEEDED" if ok else "FAILED",
            planner_used="template",
            subtasks=tuple(
                SubtaskOutcome(s.subtask_id, s.skill, s.status.value, "b0-generalist", s.output)
                for s in subs
            ),
            tokens_in=t_in,
            tokens_out=t_out,
            compute_ms=compute,
            wall_s=wall,
            contention_events=c429,
            error=next((s.error_kind for s in subs if s.error_kind), None),
        )


def session_factory(settings: Settings, echo: Callable[[str], None]) -> Callable[[ConfigSpec], Any]:
    @asynccontextmanager
    async def open_session(spec: ConfigSpec) -> AsyncIterator[CellRunner]:
        if spec.baseline == "B0":
            engine = create_async_engine(settings.bourse_database_url)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as http:
                    yield SoloCellRunner(
                        meter_llm_factory(settings, http),
                        SqlMeterRepository(engine),
                        settings.bourse_broker_delegation_deadline_s,
                        # B0 writes whole outputs with the LLM (agents use tools): the meter's
                        # global per-call cap, not the agents' smaller one (ADR-0051).
                        settings.bourse_meter_max_tokens_per_call,
                    )
            finally:
                await engine.dispose()
            return
        pool: Pool | None = None
        if spec.pool == "mixed":
            from bourse.adversaries.pool import admit_pool

            echo(f"config {spec.id}: admitting the mixed pool (ADR-0051, ADR-0053)")
            pool = await admit_pool(settings, echo)
        async with broker_from_settings(
            settings,
            echo=echo,
            policy=POLICY[spec.baseline],
            controls=controls_for(spec),
            subtask_slots=spec.subtask_slots,
        ) as session:
            yield BrokerCellRunner(
                session.broker, budget(settings), spec, pool=pool, settings=settings
            )

    return open_session


def _opt_int(value: str) -> int | None:
    return None if value in ("", "-") else int(value)


def adversary_runner(
    settings: Settings, echo: Callable[[str], None]
) -> Callable[[AdversarySpec], Any]:
    async def run(spec: AdversarySpec) -> list[AdversaryResult]:
        from bourse.adversaries.runner import SPEC_WALL_CAP, run_phase5

        started = time.monotonic()
        key = adversary_key(spec)
        report = None
        # Per-spec wall cap (ADR-0053 amendment 1): a spec can never hold a segment for hours.
        # Cleanup that must survive the cancel (selection gates) runs shielded in the runner.
        with anyio.move_on_after(spec.wall_cap_s) as scope:
            report = await run_phase5(
                settings,
                runs=spec.runs,
                only=[spec.id],
                modes=[spec.mode] if spec.mode else None,
                echo=echo,
                honest_real={spec.id: spec.honest_real} if spec.honest_real is not None else None,
                baseline_runs=spec.baseline_runs,
            )
        wall = time.monotonic() - started
        if scope.cancelled_caught or report is None:
            cap = f"{SPEC_WALL_CAP} {spec.wall_cap_s:.0f}s"
            echo(f"  {key}: {cap} reached after {wall:.0f}s; row recorded as error")
            return [
                AdversaryResult(
                    key=f"{key}#0",
                    id=spec.id,
                    mode=spec.mode or "-",
                    honest_real=spec.honest_real,
                    status="error",
                    actual=cap,
                    expected="-",
                    exposure=0,
                    exposure_real=0,
                    canaries=0,
                    first_bad=None,
                    detect_delay=None,
                    strikes=0,
                    harmed=0,
                    harmed_total=0,
                    spec_wall_s=wall,
                    finished_at=datetime.now(UTC),
                    rule="R1",
                    note=cap,
                )
            ]
        rows: list[AdversaryResult] = []
        for i, r in enumerate(report.scenarios):
            note = r.note
            if r.status == "not-exposed":
                note = spec.no_exposure_reason or (
                    "UNEXPLAINED not-exposed (no delivery, no no_exposure_reason); " + note
                )
            rows.append(
                AdversaryResult(
                    key=f"{key}#{i}",
                    id=r.id,
                    mode=r.mode,
                    honest_real=r.honest_real,
                    status="error" if r.status.startswith("error") else r.status,
                    actual=r.catch_stage,
                    expected=r.expected_stage,
                    exposure=r.exposure,
                    exposure_real=r.exposure_real,
                    canaries=r.canaries,
                    first_bad=_opt_int(r.first_bad_delegation),
                    detect_delay=_opt_int(r.detection_delay),
                    strikes=r.strikes,
                    harmed=r.harmed_tasks,
                    harmed_total=r.exposure if r.harm_total is None else r.harm_total,
                    spec_wall_s=wall,
                    finished_at=datetime.now(UTC),
                    control=r.control,
                    attempted=r.attempted,
                    signals=r.signals,
                    exposure_target=r.exposure_target,
                    rule=r.rule,
                    note=note[:300],
                )
            )
        return rows

    return run


def pii_runner(settings: Settings, http: httpx.AsyncClient) -> Callable[[str, BenchCase], Any]:
    registry = RegistryClient(http, settings.bourse_registry_url, admin_token(settings))
    budgets = MeterBudgetClient(http, settings.bourse_meter_url, meter_key(BROKER_METER_ID))
    deadline_s = settings.bourse_broker_delegation_deadline_s

    async def run(agent_id: str, case: BenchCase) -> PiiCaseResult:
        from bourse.bench.harness import pii_key

        started = time.monotonic()
        task_id = f"bench-pii-{uuid.uuid4().hex[:8]}:pii:1"
        usage_rows: list[LlmUsageRecord] | None = None
        state, output, detail = "ERROR", None, ""
        try:
            agent = await registry.get_agent(agent_id)
            if agent is None or agent.card_url is None:
                raise LookupError(f"{agent_id} not registered")
            card = card_from_dict(await fetch_card(http, agent.card_url))
            await budgets.register(
                TaskBudget(
                    task_id=task_id,
                    max_tokens=settings.bourse_task_max_tokens,
                    max_calls=settings.bourse_task_max_calls,
                )
            )
            payload = PiiIn(dataset=DatasetRef.from_csv(case.csv_text)).model_dump(mode="json")
            result = await send_task(
                card, SkillId.PII_DETECT.value, payload, task_id, deadline_s, http
            )
            state = result.state.removeprefix("TASK_STATE_")
            output, detail = result.output, result.error
        except Exception as exc:  # recorded as this case's outcome
            detail = f"{type(exc).__name__}: {exc}"[:300]
        try:
            engine = create_async_engine(settings.bourse_database_url)
            try:
                usage_rows = await SqlMeterRepository(engine).list_usage(task_id=task_id)
            finally:
                await engine.dispose()
        except Exception as exc:  # tokens are then reported as unknown (None), not as 0
            detail = (detail + f" usage unavailable: {type(exc).__name__}")[:300]
        t_in, t_out, _compute, _c429 = usage_cost(usage_rows or [])
        g = grade(SkillId.PII_DETECT, output, case.truth) if output is not None else None
        f1 = g.score if g is not None else 0.0
        return PiiCaseResult(
            key=pii_key(agent_id, case.case_id),
            agent_id=agent_id,
            case_id=case.case_id,
            state=state,
            passed=bool(g and g.passed),
            f1=f1,
            tp=g.n if g else 0,
            N=len(case.truth["pii"]["findings"]),
            detail=(g.detail if g else detail)[:300],
            wall_s=time.monotonic() - started,
            finished_at=datetime.now(UTC),
            output_sha256=None
            if output is None
            else hashlib.sha256(canonical_json(output).encode()).hexdigest(),
            tokens_in=None if usage_rows is None else t_in,
            tokens_out=None if usage_rows is None else t_out,
            llm_calls=None if usage_rows is None else len(usage_rows),
            task_max_tokens=settings.bourse_task_max_tokens,
        )

    return run
