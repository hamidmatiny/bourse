"""Bench harness (ADR-0051): plan, per-cell checkpoint, resume, grading.

The harness is transport-agnostic: a `SessionFactory` gives a `CellRunner` per config, an
`AdversaryRunner` runs one adversary spec, a `PiiRunner` sends one case straight to one PII
agent. Live wiring is in `bourse.bench.live`; tests inject fakes.

Run directory `data/bench/<name>/`: plan.json (config sha256 + ordered cell keys),
results.jsonl (one record per finished cell, flushed + fsynced), summary.json. A crash loses at
most the cell in flight; resume skips finished keys and refuses a changed config.
"""

import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from bourse.bench.split import FinalGuard, load_cases
from bourse.contracts.bench import (
    BROKER_ABLATIONS,
    AdversaryResult,
    AdversarySpec,
    BenchCase,
    BenchConfig,
    BenchRecord,
    CellResult,
    ConfigSpec,
    PiiCaseResult,
    SubtaskGrade,
)
from bourse.contracts.skills import SkillId
from bourse.grading.graders import grade

BENCH_DIR = Path("data/bench")
_RECORD = TypeAdapter[BenchRecord](BenchRecord)


class BenchRefused(Exception):
    """The config or run directory cannot be run as asked; the message says why."""


@dataclass(frozen=True)
class SubtaskOutcome:
    subtask_id: str
    skill: SkillId
    status: str
    agent_id: str | None
    output: dict[str, Any] | None


@dataclass(frozen=True)
class CellOutcome:
    run_id: str
    status: str
    planner_used: str
    subtasks: tuple[SubtaskOutcome, ...]
    tokens_in: int
    tokens_out: int
    compute_ms: float
    wall_s: float
    contention_events: int
    error: str | None = None
    # Mixed pool (6b); clean runners leave the defaults.
    pool_epoch: str | None = None
    pool_agents: tuple[str, ...] = ()  # pool member agent ids (any state)
    delegations: int = 0
    adversary_served: tuple[str, ...] = ()
    harm_lines: tuple[str, ...] = ()  # egress / unreported misreport (graders cannot see these)
    harm_agents: tuple[str, ...] = ()
    attempt_agents: tuple[str, ...] = ()  # served members that attempted (egress, misreport)
    quarantined_adversaries: tuple[str, ...] = ()


def mixed_harm(
    outcome: CellOutcome, grades: Sequence[SubtaskGrade]
) -> tuple[bool, tuple[str, ...], str]:
    """(harmed, harm agent ids, detail): a pool member's subtask failed its grader (A5), or it
    egressed / misreported unreported during the cell (ADR-0051 fix round 1)."""
    pool = set(outcome.pool_agents)
    graded = sorted({g.agent_id for g in grades if g.agent_id in pool and not g.passed})
    agents = tuple(sorted(set(graded) | set(outcome.harm_agents)))
    detail = "; ".join(
        [f"grader fail on {a}" for a in graded if a is not None] + list(outcome.harm_lines)
    )
    return bool(agents), tuple(a for a in agents if a is not None), detail[:300]


class CellRunner(Protocol):
    async def run_cell(self, case: BenchCase, repeat: int) -> CellOutcome: ...


SessionFactory = Callable[[ConfigSpec], AbstractAsyncContextManager[CellRunner]]
AdversaryRunner = Callable[[AdversarySpec], Awaitable[list[AdversaryResult]]]
PiiRunner = Callable[[str, BenchCase], Awaitable[PiiCaseResult]]


def cell_key(config_id: str, case_id: str, repeat: int) -> str:
    return f"{config_id}/{case_id}/{repeat}"


def adversary_key(spec: AdversarySpec) -> str:
    return f"adv/{spec.id}/{spec.mode or 'all'}"


def pii_key(agent: str, case_id: str) -> str:
    return f"pii/{agent}/{case_id}"


def validate_config(config: BenchConfig) -> None:
    for spec in config.configs:
        if spec.ablation is not None and spec.ablation not in BROKER_ABLATIONS:
            raise BenchRefused(
                f"{spec.id}: ablation {spec.ablation} needs a stack variant (ADR-0051); "
                "not runnable in Phase 6a"
            )


def plan_keys(
    config: BenchConfig, cases: Sequence[BenchCase], pii_cases: Sequence[BenchCase]
) -> list[str]:
    keys = [
        cell_key(spec.id, case.case_id, r)
        for spec in config.configs
        for case in cases[: spec.cases]
        for r in range(1, spec.repeats + 1)
    ]
    keys += [adversary_key(a) for a in config.adversaries]
    if config.pii_separation is not None:
        keys += [pii_key(a, c.case_id) for a in config.pii_separation.agents for c in pii_cases]
    return keys


def grade_outcome(case: BenchCase, outcome: CellOutcome) -> tuple[SubtaskGrade, ...]:
    out = []
    for sub in outcome.subtasks:
        if sub.output is None:
            out.append(
                SubtaskGrade(
                    subtask_id=sub.subtask_id,
                    skill=sub.skill.value,
                    status=sub.status,
                    agent_id=sub.agent_id,
                    passed=False,
                    n=0,
                    N=1,
                    detail="no output",
                )
            )
            continue
        g = grade(sub.skill, sub.output, case.truth)
        out.append(
            SubtaskGrade(
                subtask_id=sub.subtask_id,
                skill=sub.skill.value,
                status=sub.status,
                agent_id=sub.agent_id,
                passed=g.passed,
                n=g.n,
                N=g.N,
                detail=g.detail[:300],
            )
        )
    return tuple(out)


class Checkpoint:
    """results.jsonl: append one record per finished cell, flush + fsync before returning."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[BenchRecord]:
        if not self.path.exists():
            return []
        records: list[BenchRecord] = []
        lines = self.path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                records.append(_RECORD.validate_json(line))
            except ValidationError:
                if i == len(lines) - 1:
                    # Torn last line from a crash mid-write: drop it; that cell reruns.
                    self._rewrite(records)
                    break
                raise BenchRefused(f"{self.path}: corrupt record on line {i + 1}") from None
        return records

    def _rewrite(self, records: Sequence[BenchRecord]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            for r in records:
                fh.write(r.model_dump_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.path)

    def append(self, record: BenchRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(record.model_dump_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def prepare_run_dir(
    run_dir: Path, config: BenchConfig, keys: Sequence[str], manifest_sha256: str
) -> None:
    plan = run_dir / "plan.json"
    if plan.exists():
        data = json.loads(plan.read_text())
        if data.get("config_sha256") != config.sha256:
            raise BenchRefused(
                f"{plan}: config changed since this run started "
                f"({data.get('config_sha256')} != {config.sha256}); use a new name"
            )
        if data.get("manifest_sha256") != manifest_sha256:
            raise BenchRefused(f"{plan}: manifest changed since this run started")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": config.name,
        "split": config.split,
        "config_sha256": config.sha256,
        "manifest_sha256": manifest_sha256,
        "config": config.model_dump(mode="json"),
        "cells": list(keys),
        "created_at": datetime.now(UTC).isoformat(),
    }
    plan.write_text(json.dumps(payload, indent=2) + "\n")


@dataclass
class BenchRun:
    run_dir: Path
    records: list[BenchRecord]
    ran: int  # cells executed in this invocation
    skipped: int  # cells already finished (resume)
    manifest_sha256: str


async def run_bench(
    config: BenchConfig,
    *,
    sessions: SessionFactory,
    adversaries: AdversaryRunner | None = None,
    pii: PiiRunner | None = None,
    guard: FinalGuard | None = None,
    bench_dir: Path = BENCH_DIR,
    manifest_root: Path | None = None,
    echo: Callable[[str], None] = print,
    stop_after: int | None = None,  # tests: simulate a crash after N cells
) -> BenchRun:
    validate_config(config)
    kwargs: dict[str, Any] = {} if manifest_root is None else {"root": manifest_root}
    cases, sha = load_cases(
        config.split, guard=guard, limit=config.cases, domains=config.domains, **kwargs
    )
    pii_cases: list[BenchCase] = []
    if config.pii_separation is not None:
        sep = config.pii_separation
        pii_cases, _ = load_cases(
            config.split, guard=guard, limit=None, domains=sep.domains, **kwargs
        )
        pii_cases = pii_cases[: sep.cases]
    keys = plan_keys(config, cases, pii_cases)
    run_dir = bench_dir / config.name
    prepare_run_dir(run_dir, config, keys, sha)
    ckpt = Checkpoint(run_dir / "results.jsonl")
    records = ckpt.load()
    # Adversary rows are keyed "<spec key>#<i>"; the spec is the unit of checkpointing.
    done = {r.key.split("#", 1)[0] for r in records}
    already = len(done)
    total = len(keys)
    ran = 0
    started = time.monotonic()

    def progress(key: str) -> None:
        finished = len(done)
        elapsed = time.monotonic() - started
        eta = elapsed / ran * (total - finished) if ran else 0.0
        echo(f"[{finished}/{total}] {key} done elapsed={elapsed:.0f}s eta={eta:.0f}s")

    def budget_left() -> bool:
        return stop_after is None or ran < stop_after

    echo(
        f"bench {config.name}: split={config.split} cases={len(cases)} cells={total} "
        f"already done={len(done)} manifest={sha[:12]}"
    )
    for spec in config.configs:
        todo = [
            (case, r)
            for case in cases[: spec.cases]
            for r in range(1, spec.repeats + 1)
            if cell_key(spec.id, case.case_id, r) not in done
        ]
        if not todo or not budget_left():
            continue
        echo(
            f"config {spec.id}: baseline={spec.baseline} ablation={spec.ablation or '-'} "
            f"subtask_slots={spec.subtask_slots} cells={len(todo)}"
        )
        async with sessions(spec) as runner:
            for case, r in todo:
                if not budget_left():
                    break
                key = cell_key(spec.id, case.case_id, r)
                t0 = datetime.now(UTC)
                outcome = await runner.run_cell(case, r)
                grades = grade_outcome(case, outcome)
                mixed: dict[str, Any] = {}
                if spec.pool == "mixed":
                    harmed, harm_agents, harm_detail = mixed_harm(outcome, grades)
                    mixed = {
                        "pool": "mixed",
                        "pool_epoch": outcome.pool_epoch,
                        "delegations": outcome.delegations,
                        "adversary_served": outcome.adversary_served,
                        "harmed": harmed,
                        "harm_agents": harm_agents,
                        "harm_detail": harm_detail,
                        "attempt_agents": outcome.attempt_agents,
                        "quarantined_adversaries": outcome.quarantined_adversaries,
                    }
                result = CellResult(
                    key=key,
                    config_id=spec.id,
                    case_id=case.case_id,
                    domain=case.domain,
                    repeat=r,
                    run_id=outcome.run_id,
                    status=outcome.status,
                    planner_used=outcome.planner_used,
                    subtasks=grades,
                    tokens_in=outcome.tokens_in,
                    tokens_out=outcome.tokens_out,
                    compute_ms=outcome.compute_ms,
                    wall_s=outcome.wall_s,
                    contention_events=outcome.contention_events,
                    error=outcome.error,
                    started_at=t0,
                    finished_at=datetime.now(UTC),
                    **mixed,
                )
                ckpt.append(result)
                records.append(result)
                done.add(key)
                ran += 1
                passed = sum(s.passed for s in result.subtasks)
                echo(
                    f"  {key} {result.status} graded {passed}/{len(result.subtasks)} "
                    f"tokens={result.tokens_in}/{result.tokens_out} wall={result.wall_s:.1f}s"
                    + (
                        f" adversary {len(result.adversary_served)}/{result.delegations} "
                        f"harmed={result.harmed} {result.harm_detail}".rstrip()
                        if result.pool == "mixed"
                        else ""
                    )
                )
                progress(key)
    for adv in config.adversaries:
        key = adversary_key(adv)
        if key in done or not budget_left():
            continue
        if adversaries is None:
            raise BenchRefused("config lists adversaries but no adversary runner is wired")
        rows = await adversaries(adv)
        for row in rows:
            ckpt.append(row)
            records.append(row)
        if not rows:
            raise BenchRefused(f"{key}: adversary runner returned no rows")
        done.add(key)
        ran += 1
        progress(key)
    if config.pii_separation is not None:
        for agent in config.pii_separation.agents:
            for case in pii_cases:
                key = pii_key(agent, case.case_id)
                if key in done or not budget_left():
                    continue
                if pii is None:
                    raise BenchRefused("config has pii_separation but no PII runner is wired")
                rec = await pii(agent, case)
                ckpt.append(rec)
                records.append(rec)
                done.add(key)
                ran += 1
                progress(key)
    return BenchRun(run_dir, records, ran, already, sha)


def load_records(run_dir: Path) -> list[BenchRecord]:
    return Checkpoint(run_dir / "results.jsonl").load()


def records_of(records: Sequence[BenchRecord], kind: type[Any]) -> list[Any]:
    return [r for r in records if isinstance(r, kind)]
