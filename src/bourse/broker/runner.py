"""Wiring for the CLI and live tests: broker from settings, single runs, e2e on fixtures."""

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import create_async_engine

from bourse.agents.common.spec import METER_TASK_HEADER
from bourse.broker.delegate import DEADLINE_GRACE_S, Delegator, RegistryClient
from bourse.broker.graph import HEARTBEAT_SLACK_S, Broker
from bourse.broker.repo_sql import SqlRunRepository
from bourse.broker.select import (
    EXPLORATION,
    MeasuredConfig,
    MeasuredPolicy,
    PriorConfig,
    PriorPolicy,
    SelectionPolicy,
)
from bourse.config import Settings
from bourse.contracts.broker import DelegationState, IntakeReport
from bourse.contracts.metering import BudgetSpec
from bourse.contracts.skills_io import DatasetRef
from bourse.contracts.tasks import IntakeRequest
from bourse.grading.graders import grade
from bourse.ledger.prices import load_prices
from bourse.ledger.recorder import LedgerRecorder
from bourse.ledger.repo_sql import SqlLedgerRepository
from bourse.llm.omniroute import OpenAICompatProvider
from bourse.llm.router import Router
from bourse.llm.submit_tool import ChatClient
from bourse.mcp_servers.usage import SqlToolUsageRepository
from bourse.meter.repo_sql import SqlMeterRepository
from bourse.runtime.breaker import GLOBAL_BREAKER, CircuitBreaker
from bourse.runtime.budget import BudgetLedger
from bourse.runtime.executor import LlmOnlyExecutor
from bourse.runtime.resident import ResidentMonitor, metrics_path, monitoring, ollama_sampler
from bourse.stack.launcher import BROKER_METER_ID, admin_token, meter_key
from bourse.stack.smoke import DEFAULT_DATASET_DIR
from bourse.telemetry import setup_tracing, traced_async_client

E2E_DIR = Path("data/e2e")
E2E_REQUEST = "audit this dataset for PII, units and summary stats"
E2E_DATASETS = ("people", "measurements", "orders")


def prior_config(settings: Settings) -> PriorConfig:
    return PriorConfig(
        reliability_prior=settings.bourse_broker_reliability_prior,
        lambda_cost=settings.bourse_broker_lambda_cost,
        mu_latency=settings.bourse_broker_mu_latency,
        shadow_prices=dict(settings.bourse_broker_shadow_prices),
        latency_priors=dict(settings.bourse_broker_latency_priors),
        agent_models=dict(settings.bourse_broker_agent_models),
    )


def budget(settings: Settings) -> BudgetSpec:
    return BudgetSpec(
        max_tokens_per_call=settings.bourse_meter_max_tokens_per_call,
        max_tokens_run=settings.bourse_broker_max_tokens_run,
        wall_budget_s=settings.bourse_broker_wall_budget_s,
    )


def planner_llm_factory(
    settings: Settings, http: httpx.AsyncClient, key: str, breaker: CircuitBreaker
) -> Callable[[str], ChatClient]:
    def make(run_id: str) -> ChatClient:
        provider = OpenAICompatProvider(
            settings.bourse_meter_url,
            settings.bourse_model_large,
            key,
            http,
            provider_name="meter",
            headers={METER_TASK_HEADER: run_id},
        )
        return Router([provider], BudgetLedger(budget(settings), run_id=run_id), breaker)

    return make


def conninfo(database_url: str) -> str:
    """SQLAlchemy URL -> libpq conninfo for the LangGraph Postgres checkpointer."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass
class Session:
    broker: Broker
    ledger: SqlLedgerRepository
    monitor: ResidentMonitor


def measured_config(settings: Settings) -> MeasuredConfig:
    return MeasuredConfig(
        lambda_cost=settings.bourse_select_lambda,
        mu_latency=settings.bourse_select_mu,
        ewma_alpha=settings.bourse_select_ewma_alpha,
        min_trials=settings.bourse_select_min_trials,
        explore_every=settings.bourse_select_explore_every,
        cold_tokens=settings.bourse_select_cold_tokens,
    )


@asynccontextmanager
async def broker_from_settings(
    settings: Settings, echo: Callable[[str], None] = print, policy: str = "measured"
) -> AsyncIterator[Session]:
    """Broker wired to the local stack, with ledger, MeasuredPolicy (or PriorPolicy for
    baselines), OTel export and the resident memory guard running in the background."""
    provider = setup_tracing("bourse-broker", settings.otel_exporter_otlp_endpoint)
    engine = create_async_engine(settings.bourse_database_url)
    deadline_s = settings.bourse_broker_delegation_deadline_s
    timeout = httpx.Timeout(deadline_s + DEADLINE_GRACE_S + 30)
    try:
        async with (
            traced_async_client(timeout=timeout) as http,
            AsyncPostgresSaver.from_conn_string(conninfo(settings.bourse_database_url)) as saver,
        ):
            monitor = ResidentMonitor(
                ollama_sampler(http, settings.ollama_base_url),
                GLOBAL_BREAKER.trip,
                limit_bytes=settings.bourse_resident_limit_bytes,
                sustain_s=settings.bourse_resident_sustain_s,
                interval_s=settings.bourse_resident_interval_s,
                out=metrics_path(),
            )
            await saver.setup()
            runs = SqlRunRepository(engine)
            usage = SqlMeterRepository(engine)
            ledger = SqlLedgerRepository(engine)
            recorder = LedgerRecorder(
                ledger,
                usage,
                SqlToolUsageRepository(engine),
                load_prices(Path(settings.bourse_prices_file)),
            )
            registry = RegistryClient(http, settings.bourse_registry_url, admin_token(settings))
            delegator = Delegator(
                registry, http, runs, GLOBAL_BREAKER, deadline_s=deadline_s, ledger=recorder
            )
            priors = prior_config(settings)
            chosen_policy: SelectionPolicy = (
                PriorPolicy(priors)
                if policy == "prior"
                else MeasuredPolicy(measured_config(settings), priors, ledger)
            )
            broker = Broker(
                planner_llm=planner_llm_factory(
                    settings, http, meter_key(BROKER_METER_ID), GLOBAL_BREAKER
                ),
                registry=registry,
                delegator=delegator,
                policy=chosen_policy,
                runs=runs,
                usage=usage,
                executor=LlmOnlyExecutor(settings.ollama_num_parallel),
                breaker=GLOBAL_BREAKER,
                checkpointer=saver,
                wall_budget_s=settings.bourse_broker_wall_budget_s,
                heartbeat_timeout_s=deadline_s + DEADLINE_GRACE_S + HEARTBEAT_SLACK_S,
                echo=echo,
                ledger=ledger,
            )
            delegator.heartbeat = broker.beat
            async with monitoring(monitor):
                yield Session(broker, ledger, monitor)
    finally:
        await engine.dispose()
        if provider is not None:
            provider.shutdown()


def load_fixture(name: str, dataset_dir: Path = DEFAULT_DATASET_DIR) -> tuple[str, dict[str, Any]]:
    csv_text = (dataset_dir / f"{name}.csv").read_text()
    truth: dict[str, Any] = json.loads((dataset_dir / f"{name}.truth.json").read_text())
    return csv_text, truth


def intake(settings: Settings, csv_text: str, request_text: str) -> IntakeRequest:
    return IntakeRequest(
        dataset=DatasetRef.from_csv(csv_text), request_text=request_text, budget=budget(settings)
    )


@dataclass
class SubtaskRow:
    subtask: str
    skill: str
    agent: str
    attempts: int
    state: str
    passed: bool
    n: int
    N: int  # n/N reporting convention
    tokens_in: int
    tokens_out: int
    wall_s: float


async def subtask_rows(
    broker: Broker, report: IntakeReport, truth: dict[str, Any]
) -> list[SubtaskRow]:
    rows: list[SubtaskRow] = []
    for r in report.subtasks:
        g = grade(r.skill, r.output, truth) if r.output is not None else None
        usage = [
            u
            for u in await broker.usage.list_usage(run_id=report.run_id)
            if (u.task_id or "").startswith(f"{report.run_id}:{r.subtask_id}:")
        ]
        wall_ms = sum(d.wall_ms for d in report.delegations if d.subtask_id == r.subtask_id)
        rows.append(
            SubtaskRow(
                subtask=r.subtask_id,
                skill=r.skill.value,
                agent=r.agent_id or "-",
                attempts=r.attempts,
                state=r.status.value,
                passed=bool(g and g.passed),
                n=g.n if g else 0,
                N=g.N if g else 0,
                tokens_in=sum(u.tokens_in for u in usage),
                tokens_out=sum(u.tokens_out for u in usage),
                wall_s=round(wall_ms / 1000, 1),
            )
        )
    return rows


def subtask_table(rows: list[SubtaskRow]) -> list[list[str]]:
    out = [["subtask", "skill", "agent", "attempts", "state", "passed", "n/N", "tokens", "wall_s"]]
    for r in rows:
        out.append(
            [
                r.subtask,
                r.skill,
                r.agent,
                str(r.attempts),
                r.state,
                str(r.passed),
                f"{r.n}/{r.N}",
                f"{r.tokens_in}/{r.tokens_out}",
                str(r.wall_s),
            ]
        )
    return out


@dataclass
class E2ERow:
    dataset: str
    repeat: int
    run_id: str
    trace_id: str | None
    status: str
    planner_used: str
    fallbacks: int
    exploration_picks: int
    selections: dict[str, str | None]  # skill -> chosen agent
    spotcheck_failed: int
    spotcheck_total: int
    shadow_usd: float
    subtasks_passed: int
    subtasks_total: int
    tokens_in: int
    tokens_out: int
    wall_s: float
    subtasks: list[dict[str, Any]]


def fallbacks_used(report: IntakeReport) -> int:
    """Subtasks that needed more than one attempt."""
    return sum(r.attempts > 1 for r in report.subtasks)


async def run_row(
    session: Session, report: IntakeReport, truth: dict[str, Any], dataset: str, repeat: int
) -> E2ERow:
    srows = await subtask_rows(session.broker, report, truth)
    ledger_rows = await session.ledger.list_rows(run_id=report.run_id)
    selections = await session.ledger.list_selections(run_id=report.run_id)
    checked = [r for r in ledger_rows if r.spot_check_passed is not None]
    return E2ERow(
        dataset=dataset,
        repeat=repeat,
        run_id=report.run_id,
        trace_id=report.trace_id,
        status=report.status.value,
        planner_used=report.planner_used,
        fallbacks=fallbacks_used(report),
        exploration_picks=sum(s.reason == EXPLORATION for s in selections),
        selections={s.skill.value: s.chosen for s in selections},
        spotcheck_failed=sum(r.state is DelegationState.FAILED_SPOTCHECK for r in ledger_rows),
        spotcheck_total=len(checked),
        shadow_usd=sum(r.shadow_usd for r in ledger_rows),
        subtasks_passed=sum(r.passed for r in srows),
        subtasks_total=len(srows),
        tokens_in=report.tokens_in,
        tokens_out=report.tokens_out,
        wall_s=round(report.wall_ms / 1000, 1),
        subtasks=[asdict(r) for r in srows],
    )


async def e2e_all(
    settings: Settings,
    datasets: tuple[str, ...] = E2E_DATASETS,
    echo: Callable[[str], None] = print,
    repeat: int = 1,
) -> tuple[list[E2ERow], Path, int]:
    """Returns (rows, written json, resident peak bytes)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows: list[E2ERow] = []
    plan = [(r, ds) for r in range(1, repeat + 1) for ds in datasets]
    started = time.monotonic()
    async with broker_from_settings(settings, echo=echo) as session:
        for i, (rep, ds) in enumerate(plan, start=1):
            csv_text, truth = load_fixture(ds)
            echo(f"[{i}/{len(plan)}] {ds} repeat {rep}: start")
            report = await session.broker.start(intake(settings, csv_text, E2E_REQUEST))
            row = await run_row(session, report, truth, ds, rep)
            rows.append(row)
            elapsed = time.monotonic() - started
            eta = elapsed / i * (len(plan) - i)
            echo(
                f"[{i}/{len(plan)}] {ds} {row.status} planner={row.planner_used} "
                f"graded {row.subtasks_passed}/{row.subtasks_total} fallbacks={row.fallbacks} "
                f"explore={row.exploration_picks} spotcheck_failed={row.spotcheck_failed}/"
                f"{row.spotcheck_total} tokens={row.tokens_in}/{row.tokens_out} "
                f"shadow_usd={row.shadow_usd:.6f} wall={row.wall_s}s trace={row.trace_id} "
                f"eta={eta:.0f}s"
            )
        peak = session.monitor.peak
    out = E2E_DIR / f"e2e-{stamp}.json"
    await anyio.Path(E2E_DIR).mkdir(parents=True, exist_ok=True)
    payload = {"resident_peak_bytes": peak, "runs": [asdict(r) for r in rows]}
    await anyio.Path(out).write_text(json.dumps(payload, indent=2) + "\n")
    return rows, out, peak


def selection_share(rows: list[E2ERow]) -> list[str]:
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        for skill, agent in r.selections.items():
            per = counts.setdefault(skill, {})
            per[agent or "-"] = per.get(agent or "-", 0) + 1
    lines = []
    for skill in sorted(counts):
        total = sum(counts[skill].values())
        shares = ", ".join(f"{a} {n}/{total}" for a, n in sorted(counts[skill].items()))
        lines.append(f"selection {skill}: {shares}")
    return lines


def e2e_summary(rows: list[E2ERow], resident_peak: int, limit: int) -> list[str]:
    n = len(rows)
    return [
        f"runs succeeded {sum(r.status == 'SUCCEEDED' for r in rows)}/{n}",
        f"planner_used llm {sum(r.planner_used == 'llm' for r in rows)}/{n}",
        f"fallbacks used {sum(r.fallbacks for r in rows)}",
        *selection_share(rows),
        f"exploration picks {sum(r.exploration_picks for r in rows)}",
        f"spot-check failures {sum(r.spotcheck_failed for r in rows)}/"
        f"{sum(r.spotcheck_total for r in rows)}",
        f"subtasks graded passed {sum(r.subtasks_passed for r in rows)}/"
        f"{sum(r.subtasks_total for r in rows)}",
        f"tokens in {sum(r.tokens_in for r in rows)} out {sum(r.tokens_out for r in rows)}",
        f"total shadow_usd {sum(r.shadow_usd for r in rows):.6f}",
        f"resident peak {resident_peak} (limit {limit})",
        *(f"trace {r.dataset} repeat {r.repeat} {r.trace_id}" for r in rows),
    ]
