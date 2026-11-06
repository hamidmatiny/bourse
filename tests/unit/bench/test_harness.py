"""Harness: resume after a simulated crash, torn checkpoint line, config drift, aggregation."""

from pathlib import Path
from typing import Any

import pytest

from bourse.bench.harness import BenchRefused, Checkpoint, load_records, run_bench
from bourse.bench.report import format_summary, sizing_lines, summarize
from bourse.bench.split import write_manifest
from bourse.contracts.bench import BenchConfig, CellResult, Provenance
from tests.bench_fakes import ScriptedSessions, scripted_adversary, scripted_pii


def _cfg(**kw: Any) -> BenchConfig:
    base: dict[str, Any] = {
        "name": "unit",
        "split": "dev",
        "cases": 3,
        "configs": [
            {"id": "B3", "baseline": "B3", "subtask_slots": 2},
            {"id": "B3-serial", "baseline": "B3", "subtask_slots": 1},
            {"id": "B2", "baseline": "B2"},
        ],
        "adversaries": [{"id": "A5a", "mode": "sole-provider", "runs": 12, "honest_real": 2}],
        "pii_separation": {
            "agents": ["vendor-c-pii-fast@vendor-c.local", "vendor-b-pii-precise@vendor-b.local"],
            "cases": 2,
        },
    }
    return BenchConfig.model_validate(base | kw)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_manifest("dev", 12, "unit-seed", tmp_path / "manifests")
    return tmp_path


def _prov(cfg: BenchConfig) -> Provenance:
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
        config_sha256=cfg.sha256,
        at=datetime.now(UTC),
    )


async def _run(cfg: BenchConfig, root: Path, sessions: ScriptedSessions, **kw: Any) -> Any:
    return await run_bench(
        cfg,
        sessions=sessions,
        adversaries=scripted_adversary,
        pii=scripted_pii,
        bench_dir=root / "bench",
        manifest_root=root / "manifests",
        echo=lambda _l: None,
        **kw,
    )


async def test_full_run_counts_every_cell(root: Path) -> None:
    cfg = _cfg()
    res = await _run(cfg, root, ScriptedSessions())
    cells = [r for r in res.records if isinstance(r, CellResult)]
    assert len(cells) == 9  # 3 configs x 3 cases
    assert res.ran == 9 + 1 + 4  # cells + adversary spec + 2 agents x 2 pii cases
    assert all(c.all_passed for c in cells)


async def test_resume_after_crash_loses_at_most_the_cell_in_flight(root: Path) -> None:
    cfg = _cfg()
    sessions = ScriptedSessions(crash_on="B3-serial/dev-0001-orders/1")
    with pytest.raises(RuntimeError, match="simulated crash"):
        await _run(cfg, root, sessions)
    done = {r.key for r in load_records(root / "bench" / "unit")}
    assert done == {
        "B3/dev-0000-people/1",
        "B3/dev-0001-orders/1",
        "B3/dev-0002-measurements/1",
        "B3-serial/dev-0000-people/1",
    }
    again = ScriptedSessions()
    res = await _run(cfg, root, again)
    assert res.skipped == 4
    reran = [k for r in again.runners for k in r.ran]
    assert reran[0] == "B3-serial/dev-0001-orders/1"  # the crashed cell reruns first
    assert not set(reran) & done  # nothing finished is rerun
    keys = [r.key for r in res.records]
    assert len(keys) == len(set(keys)) == 9 + 1 + 4


async def test_stop_after_then_resume_runs_rest(root: Path) -> None:
    cfg = _cfg()
    first = await _run(cfg, root, ScriptedSessions(), stop_after=5)
    assert first.ran == 5
    second = await _run(cfg, root, ScriptedSessions())
    assert second.skipped == 5 and second.ran == 9
    third = await _run(cfg, root, ScriptedSessions())
    assert third.ran == 0 and third.skipped == 14


async def test_torn_last_line_is_dropped_and_rerun(root: Path) -> None:
    cfg = _cfg(adversaries=[], pii_separation=None)
    await _run(cfg, root, ScriptedSessions(), stop_after=2)
    path = root / "bench" / "unit" / "results.jsonl"
    path.write_text(path.read_text() + '{"record": "cell", "key": "B3/dev-00')
    records = Checkpoint(path).load()
    assert len(records) == 2
    assert path.read_text().count("\n") == 2


async def test_changed_config_is_refused_on_resume(root: Path) -> None:
    await _run(_cfg(), root, ScriptedSessions(), stop_after=1)
    with pytest.raises(BenchRefused, match="config changed"):
        await _run(_cfg(cases=2), root, ScriptedSessions())


async def test_stack_variant_ablations_are_refused_not_faked(root: Path) -> None:
    cfg = _cfg(configs=[{"id": "B3-no-exam", "baseline": "B3", "ablation": "no-exam"}])
    with pytest.raises(BenchRefused, match="stack variant"):
        await _run(cfg, root, ScriptedSessions())
    with pytest.raises(ValueError, match="B3 only"):
        _cfg(configs=[{"id": "x", "baseline": "B1", "ablation": "no-canary"}])


async def test_test_split_refused_without_guard(root: Path) -> None:
    write_manifest("test", 4, "t", root / "manifests")
    from bourse.bench.split import SplitError

    with pytest.raises(SplitError, match="--final"):
        await _run(_cfg(split="test"), root, ScriptedSessions())


async def test_aggregation_n_over_n_speedup_and_sizing(root: Path) -> None:
    cfg = _cfg()
    res = await _run(cfg, root, ScriptedSessions())
    summary = summarize(cfg, res.records, _prov(cfg))
    by = {c.config_id: c for c in summary.configs}
    assert (by["B3"].task_success.n, by["B3"].task_success.N) == (3, 3)
    assert (by["B3"].subtask_pass.n, by["B3"].subtask_pass.N) == (15, 15)
    assert by["B3"].latency_s_p50.value == 5.0 and by["B3-serial"].latency_s_p50.value == 10.0
    (sp,) = summary.speedups
    assert (sp.serial_config, sp.parallel_config, sp.paired_cases) == ("B3-serial", "B3", 3)
    assert sp.total_ratio == pytest.approx(2.0)
    assert summary.adversaries is not None
    assert (summary.adversaries.caught.n, summary.adversaries.caught.N) == (1, 1)
    assert summary.adversary_rows[0].honest_real == 2
    pii = {p.agent_id: p for p in summary.pii_agents}
    assert pii["vendor-b-pii-precise@vendor-b.local"].passed.n == 2
    assert pii["vendor-c-pii-fast@vendor-c.local"].passed.n == 0
    text = "\n".join(format_summary(summary))
    assert "3/3" in text and "speedup B3-serial -> B3" in text
    sizing = "\n".join(sizing_lines(summary, hours=8.0))
    assert "B3-serial  3  10.0" in sizing and "N <= " in sizing
    assert summary.pii_identical is not None
    assert (summary.pii_identical.n, summary.pii_identical.N) == (0, 2)
    assert "byte-identical across agents (output_sha256): 0/2" in text


async def test_config_rows_have_one_cell_per_column_and_no_inline_n(root: Path) -> None:
    """Regression: `N=5` followed by a two-space gap and `95.0` read as `N=595.0` in a paste."""
    from bourse.bench.report import CONFIG_COLUMNS

    cfg = _cfg()
    res = await _run(cfg, root, ScriptedSessions())
    lines = format_summary(summarize(cfg, res.records, _prov(cfg)))
    header = lines.index(" | ".join(CONFIG_COLUMNS))
    rows = lines[header + 1 : header + 1 + len(cfg.configs)]
    for row in rows:
        cells = row.split(" | ")
        assert len(cells) == len(CONFIG_COLUMNS), row
        assert "N=" not in row, row
    b3_row = rows[[c.id for c in cfg.configs].index("B3")]
    b3 = dict(zip(CONFIG_COLUMNS, b3_row.split(" | "), strict=True))
    assert b3["N"] == "3" and b3["task_success n/N [95% CI]"].startswith("3/3 ")
    assert b3["latency_s p50"] == "5.0 [-, -]"


def test_pii_identical_counts_only_cases_hashed_by_every_agent() -> None:
    from datetime import UTC, datetime

    from bourse.bench.report import pii_identical
    from bourse.contracts.bench import PiiCaseResult

    def row(agent: str, case: str, h: str | None) -> PiiCaseResult:
        return PiiCaseResult(
            key=f"pii/{agent}/{case}", agent_id=agent, case_id=case, state="COMPLETED",
            passed=False, f1=0.8, tp=1, N=2, detail="", wall_s=1.0,
            finished_at=datetime.now(UTC), output_sha256=h,
        )  # fmt: skip

    rows = [
        row("a", "c1", "x"), row("b", "c1", "x"),  # identical
        row("a", "c2", "x"), row("b", "c2", "y"),  # differ
        row("a", "c3", None), row("b", "c3", "y"),  # pre-hash record: excluded
    ]  # fmt: skip
    p = pii_identical(rows)
    assert p is not None and (p.n, p.N) == (1, 2)
    assert pii_identical([row("a", "c1", "x")]) is None


async def test_pii_only_config_prints_no_empty_config_table(root: Path) -> None:
    from bourse.bench.report import CONFIG_COLUMNS

    cfg = _cfg(configs=[], adversaries=[])
    res = await _run(cfg, root, ScriptedSessions())
    lines = format_summary(summarize(cfg, res.records, _prov(cfg)))
    assert " | ".join(CONFIG_COLUMNS) not in lines
    assert any(line.startswith("pii outputs byte-identical") for line in lines)


async def test_per_config_cases_runs_the_first_n_only(root: Path) -> None:
    """ADR-0051: B0 runs N=50 where the others run N=100 — the first N of the same cases."""
    cfg = _cfg(
        configs=[
            {"id": "B0", "baseline": "B0", "cases": 2},
            {"id": "B3", "baseline": "B3"},
        ],
        adversaries=[],
        pii_separation=None,
    )
    res = await _run(cfg, root, ScriptedSessions())
    cells = [r for r in res.records if isinstance(r, CellResult)]
    b0 = [c.case_id for c in cells if c.config_id == "B0"]
    b3 = [c.case_id for c in cells if c.config_id == "B3"]
    assert len(b0) == 2 and len(b3) == 3 and b0 == b3[:2]
    again = await _run(cfg, root, ScriptedSessions())
    assert again.ran == 0 and again.skipped == 5
