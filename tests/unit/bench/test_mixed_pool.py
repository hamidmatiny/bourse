"""Mixed-pool configs (B1-mix / B3-mix, ADR-0051 fix round 1, ADR-0053): harm accounting."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from bourse.adversaries.pool import foreign_vendor_x, pool_agent_ids
from bourse.bench.harness import CellOutcome, CellRunner, SubtaskOutcome, mixed_harm, run_bench
from bourse.bench.report import format_summary, summarize
from bourse.bench.split import write_manifest
from bourse.contracts.bench import (
    MIX_POOL,
    BenchCase,
    BenchConfig,
    CellResult,
    ConfigSpec,
    SubtaskGrade,
)
from bourse.contracts.skills import SkillId
from tests.bench_fakes import truth_output
from tests.unit.bench.test_harness import _prov
from tests.unit.bench.test_preflight import rec

SLEEPER = "sleeper-loud@vendor-x.local"
EXFIL = "exfil-http@vendor-x.local"
HONEST = "vendor-c-pii-fast@vendor-c.local"


def test_pool_ids_match_the_adr_list() -> None:
    ids = pool_agent_ids()
    assert sorted(ids.values()) == sorted(MIX_POOL)
    assert all(a.endswith("@vendor-x.local") for a in ids)


def test_foreign_active_vendor_x_agents_block_the_pool() -> None:
    pool = frozenset(pool_agent_ids())
    agents = [rec(SLEEPER), rec("token-burner-raw@vendor-x.local"), rec(HONEST)]
    assert foreign_vendor_x(agents, pool) == ["token-burner-raw@vendor-x.local"]
    quarantined = [rec("token-burner-raw@vendor-x.local", "QUARANTINED")]
    assert foreign_vendor_x(quarantined, pool) == []


def _grade(agent: str, passed: bool) -> SubtaskGrade:
    return SubtaskGrade(
        subtask_id="pii_check",
        skill="pii.detect",
        status="COMPLETED",
        agent_id=agent,
        passed=passed,
        n=1,
        N=1,
    )


def _outcome(**kw: Any) -> CellOutcome:
    base: dict[str, Any] = {
        "run_id": "r",
        "status": "SUCCEEDED",
        "planner_used": "template",
        "subtasks": (),
        "tokens_in": 1,
        "tokens_out": 1,
        "compute_ms": 1.0,
        "wall_s": 1.0,
        "contention_events": 0,
        "pool_agents": (SLEEPER, EXFIL),
    }
    return CellOutcome(**(base | kw))


def test_mixed_harm_from_grader_and_events() -> None:
    harmed, agents, detail = mixed_harm(_outcome(), [_grade(SLEEPER, False)])
    assert harmed and agents == (SLEEPER,) and "grader fail on " + SLEEPER in detail
    # An honest agent failing its grader is not adversary harm.
    assert mixed_harm(_outcome(), [_grade(HONEST, False)])[0] is False
    ok = mixed_harm(_outcome(), [_grade(SLEEPER, True)])
    assert ok == (False, (), "")
    egress = mixed_harm(
        _outcome(harm_lines=("A6a egress events 1",), harm_agents=(EXFIL,)), [_grade(HONEST, True)]
    )
    assert egress[0] and egress[1] == (EXFIL,) and "egress" in egress[2]


class _MixedRunner:
    """Cell 1: sleeper serves pii and passes. Cell 2: sleeper serves and fails its grader.
    Cell 3: exfil serves schema and egresses into the sinkhole (full stack: an attempt, not
    harm, ADR-0053 amendment 1); sleeper is QUARANTINED at the end of cell 3."""

    def __init__(self) -> None:
        self.i = 0

    async def run_cell(self, case: BenchCase, repeat: int) -> CellOutcome:
        self.i += 1
        subs = []
        for s in SkillId:
            out = truth_output(s, case.truth)
            agent = HONEST
            if s is SkillId.PII_DETECT and self.i in (1, 2):
                agent = SLEEPER
                if self.i == 2:
                    out = {"findings": []}
            if s is SkillId.SCHEMA_PROFILE and self.i == 3:
                agent = EXFIL
            subs.append(SubtaskOutcome(s.value, s, "COMPLETED", agent, out))
        served = {1: (SLEEPER,), 2: (SLEEPER,), 3: (EXFIL,)}[self.i]
        return _outcome(
            run_id=f"r{self.i}",
            subtasks=tuple(subs),
            pool_epoch="2026-11-06T00:00:00+00:00",
            delegations=5,
            adversary_served=served,
            attempt_agents=(EXFIL,) if self.i == 3 else (),
            quarantined_adversaries=(SLEEPER,) if self.i == 3 else (),
        )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_manifest("dev", 12, "unit-seed", tmp_path / "manifests")
    return tmp_path


async def test_mixed_config_records_harm_share_and_detection(root: Path) -> None:
    cfg = BenchConfig(
        name="mix",
        split="dev",
        cases=3,
        configs=(ConfigSpec(id="B3-mix", baseline="B3", pool="mixed"),),
    )
    runner = _MixedRunner()

    @asynccontextmanager
    async def sessions(spec: ConfigSpec) -> AsyncIterator[CellRunner]:
        yield runner

    run = await run_bench(
        cfg,
        sessions=sessions,
        bench_dir=root / "bench",
        manifest_root=root / "manifests",
        echo=lambda _l: None,
    )
    cells = [r for r in run.records if isinstance(r, CellResult) and r.pool == "mixed"]
    assert [c.harmed for c in cells] == [False, True, False]
    assert cells[2].attempt_agents == (EXFIL,)
    assert cells[0].pool_epoch == "2026-11-06T00:00:00+00:00"
    summary = summarize(cfg, run.records, _prov(cfg))
    c = summary.configs[0]
    assert c.harmed is not None and (c.harmed.n, c.harmed.N) == (1, 3)
    assert c.adversary_share is not None and (c.adversary_share.n, c.adversary_share.N) == (3, 15)
    by = {p.agent_id: p for p in c.pool_adversaries}
    assert (by[SLEEPER].served, by[SLEEPER].harmed) == (2, 1)
    assert (by[SLEEPER].first_harm_cell, by[SLEEPER].quarantined_cell) == (2, 3)
    assert by[SLEEPER].detect_delay_cells == 1
    assert (by[EXFIL].first_harm_cell, by[EXFIL].quarantined_cell) == (None, None)
    assert (by[EXFIL].attempted, by[EXFIL].harmed) == (1, 0)
    text = "\n".join(format_summary(summary))
    assert "mixed pool B3-mix: honest success (all subtasks pass) 2/3" in text
    assert "harmed cases 1/3" in text and "adversary share of delegations 3/15" in text
    assert (
        f"{SLEEPER}: served 2 | attempted cells 0 | harmed cells 1 | first harm cell 2 | "
        "quarantined at cell 3" in text
    )
