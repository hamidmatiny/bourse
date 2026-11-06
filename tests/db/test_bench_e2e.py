"""Hermetic end-to-end bench run (ADR-0051): one tiny run of every baseline with a fake LLM.

Real: Postgres registry (SqlRepository, migrations), registry app, LangGraph broker, Delegator,
spot checks, canaries, MeasuredPolicy / PriorPolicy / RandomPolicy, the bench harness
(plan, checkpoint, resume, grading, aggregation, sizing) and the live cell runners
(`BrokerCellRunner`, `SoloCellRunner`). Fake: planner LLM (fake meter), honest A2A agents that
answer from the case truth, B0's LLM (`TruthChat`), the adversary runner. No Ollama.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bourse.agents.common.executor import ParsedRequest
from bourse.bench.generator import generate_case
from bourse.bench.harness import CellRunner, load_records, run_bench
from bourse.bench.live import NO_TRUST, BrokerCellRunner, SoloCellRunner, controls_for
from bourse.bench.report import format_summary, sizing_lines, summarize
from bourse.bench.split import write_manifest
from bourse.broker.delegate import RegistryClient
from bourse.broker.select import MeasuredConfig, MeasuredPolicy, PriorConfig, PriorPolicy
from bourse.config import get_settings
from bourse.contracts.bench import BenchCase, BenchConfig, CellResult, ConfigSpec, Provenance
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.registry.repo_sql import SqlRepository
from bourse.runtime.executor import PoolExecutor
from tests.bench_fakes import TruthChat, scripted_adversary, truth_output
from tests.broker_harness import (
    ADMIN,
    REGISTRY,
    SPEC,
    BrokerBed,
    FakeAgent,
    Outcome,
    honest,
    one_agent_per_skill,
)
from tests.registry_helpers import make_party

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "vendors",
    "agents",
    "card_revisions",
    "violations",
    "state_transitions",
    "violation_aggregates",
)
N_CASES = 4  # people, orders, measurements, clinic


def _alembic() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(URL)
    await _truncate(eng)
    yield eng
    await _truncate(eng)
    await eng.dispose()


def _cases() -> dict[str, BenchCase]:
    cases = [generate_case("dev", "e2e", i) for i in range(N_CASES)]
    return {c.model_dump()["csv_text"]: c for c in cases}


def _truth_agent(by_csv: dict[str, BenchCase]) -> Any:
    async def behave(req: ParsedRequest) -> Outcome:
        case = by_csv.get(req.payload.dataset.csv_text)
        if case is None:  # canary (exam-bank dataset): answer like an honest agent
            return await honest(req)
        return Outcome(output=truth_output(req.skill, case.truth))

    return behave


CONFIG = BenchConfig.model_validate(
    {
        "name": "e2e",
        "split": "dev",
        "cases": N_CASES,
        "configs": [
            {"id": "B0", "baseline": "B0"},
            {"id": "B1", "baseline": "B1"},
            {"id": "B2", "baseline": "B2"},
            {"id": "B3", "baseline": "B3", "subtask_slots": 2},
            {"id": "B3-serial", "baseline": "B3", "subtask_slots": 1},
            {"id": "B3-no-spotcheck", "baseline": "B3", "ablation": "no-spotcheck"},
        ],
        "adversaries": [{"id": "A5a", "mode": "sole-provider", "runs": 12, "honest_real": 3}],
    }
)


async def test_tiny_bench_run_all_baselines(engine: AsyncEngine, tmp_path: Path) -> None:
    write_manifest("dev", N_CASES, "e2e", tmp_path / "manifests")
    by_csv = _cases()
    behave = _truth_agent(by_csv)
    agents = one_agent_per_skill(
        a_profile=behave, a_stats=behave, a_pii=behave, a_units=behave, a_report=behave
    )
    # A second PII vendor so B1/B3 selection has a real choice.
    agents.append(FakeAgent("b-pii", make_party("vendor-y.local"), (SkillId.PII_DETECT,), behave))
    bed = await BrokerBed(agents, repo=SqlRepository(engine), canary_every=2).setup()
    try:

        @asynccontextmanager
        async def sessions(spec: ConfigSpec) -> AsyncIterator[CellRunner]:
            if spec.baseline == "B0":
                yield _B0ByCase(by_csv, bed)
                return
            broker = bed.broker()
            ctl = controls_for(spec)
            broker.delegator.spot_checks = ctl.spot_checks
            broker.delegator.report_violations = ctl.report_violations
            if not ctl.canaries:
                broker.canaries = None
            broker.executor = PoolExecutor(spec.subtask_slots)
            if spec.baseline == "B3":
                broker.policy = MeasuredPolicy(MeasuredConfig(), PriorConfig(), bed.ledger)
            yield BrokerCellRunner(broker, SPEC, spec)

        res = await run_bench(
            CONFIG,
            sessions=sessions,
            adversaries=scripted_adversary,
            bench_dir=tmp_path / "bench",
            manifest_root=tmp_path / "manifests",
            echo=lambda _l: None,
        )
        cells = [r for r in res.records if isinstance(r, CellResult)]
        assert len(cells) == 6 * N_CASES
        by_config: dict[str, list[CellResult]] = {}
        for c in cells:
            by_config.setdefault(c.config_id, []).append(c)
        for cid, rows in by_config.items():
            assert all(r.status == "SUCCEEDED" for r in rows), (cid, [r.error for r in rows])
            assert all(r.all_passed for r in rows), (
                cid,
                [s.detail for r in rows for s in r.subtasks],
            )
        # B2: priors tie (one model), so each task's PII vendor is a seeded draw per
        # (case, repeat, subtask) — reproducible from the case seed, not agent_id order.
        pii_b2 = [
            (r.case_id, s.subtask_id, s.agent_id)
            for r in by_config["B2"]
            for s in r.subtasks
            if s.skill == "pii.detect"
        ]
        pii_agents = await RegistryClient(bed.http, REGISTRY, ADMIN).active_agents(
            SkillId.PII_DETECT
        )
        assert len(pii_agents) == 2
        seeds = {c.case_id: c.seed for c in by_csv.values()}
        expected = [
            (
                cid,
                sid,
                PriorPolicy(PriorConfig(), tie_seed=f"{seeds[cid]}:1")
                .choose(Subtask(id=sid, skill=SkillId.PII_DETECT, input_ref="dataset"), pii_agents)
                .chosen,
            )
            for cid, sid, _ in pii_b2
        ]
        assert pii_b2 == expected
        # No trust controls in B1/B2: no spot check on any of their delegations; B3 checks all.
        for cid in ("B1", "B2", "B3", "B3-no-spotcheck"):
            run_ids = {r.run_id for r in by_config[cid]}
            ledger_rows = [r for r in await bed.ledger.list_rows() if r.run_id in run_ids]
            checked = [r.spot_check_passed for r in ledger_rows]
            assert ledger_rows, cid
            if cid == "B3":
                assert all(c is True for c in checked), cid
            else:
                assert all(c is None for c in checked), (cid, checked)
        # Canaries only where trust controls are on (B3*); none in B1/B2.
        canary_rows = [r for r in await bed.ledger.list_rows() if r.kind == "canary"]
        assert canary_rows, "B3 sends canaries"
        assert NO_TRUST.canaries is False

        # Resume: nothing left to run.
        again = await run_bench(
            CONFIG,
            sessions=sessions,
            adversaries=scripted_adversary,
            bench_dir=tmp_path / "bench",
            manifest_root=tmp_path / "manifests",
            echo=lambda _l: None,
        )
        assert again.ran == 0 and again.skipped == 6 * N_CASES + 1

        records = load_records(tmp_path / "bench" / "e2e")
        prov = _prov()
        summary = summarize(CONFIG, records, prov)
        by = {c.config_id: c for c in summary.configs}
        for cid in by:
            assert (by[cid].task_success.n, by[cid].task_success.N) == (N_CASES, N_CASES)
            assert (by[cid].grader_pass.n, by[cid].grader_pass.N) == (N_CASES, N_CASES)
        # Speedup pairs only the same baseline + ablation (slots 1 vs 2).
        assert [(s.serial_config, s.parallel_config) for s in summary.speedups] == [
            ("B3-serial", "B3")
        ]
        lines = format_summary(summary) + sizing_lines(summary, hours=8.0)
        assert any(line.startswith("B0 | 4 | 4/4 ") for line in lines), lines
        assert any("N <= " in line for line in lines)
    finally:
        await bed.aclose()


class _B0ByCase:
    """SoloCellRunner whose fake LLM answers from the case being run."""

    def __init__(self, by_csv: dict[str, BenchCase], bed: BrokerBed) -> None:
        self.by_csv = by_csv
        self.bed = bed

    async def run_cell(self, case: BenchCase, repeat: int) -> Any:
        chat = TruthChat(case.truth)
        runner = SoloCellRunner(lambda _tid: chat, self.bed.usage, deadline_s=10.0, max_tokens=2048)
        out = await runner.run_cell(case, repeat)
        assert chat.calls == [
            "pii.detect", "schema.profile", "units.normalize", "stats.compute", "report.compose"
        ]  # dependency levels, ids sorted within a level  # fmt: skip
        return out


def _prov() -> Provenance:
    from datetime import UTC, datetime

    return Provenance(
        final=False,
        git_sha="x",
        git_dirty=True,
        manifest_sha256=None,
        model_digests={},
        ollama_env={},
        generator_version="1",
        grader_version="1",
        config_sha256=CONFIG.sha256,
        at=datetime.now(UTC),
    )
