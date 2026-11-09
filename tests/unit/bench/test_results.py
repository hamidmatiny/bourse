"""ADR-0054: tracked results/ export and the generated RESULTS.md."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bourse.bench.results_export import (
    ExportRefused,
    check_provenance,
    evidence_digest,
    phase5c_evidence,
)
from bourse.bench.results_report import (
    README_BEGIN,
    README_END,
    RenderError,
    fill,
    harm_types,
    poison_identity,
    render,
    render_all,
    update_readme,
)
from bourse.contracts.bench import CellResult, Domain, Provenance, SubtaskGrade

REPO = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 11, 7, tzinfo=UTC)
POISON = "poison-quiet@vendor-x.local"
FAST = "vendor-c-pii-fast@vendor-c.local"


def cell(
    case: str,
    *,
    domain: Domain = "people",
    pii_agent: str = FAST,
    pii_passed: bool = True,
    detail: str = "tp=1 fp=0 fn=0",
    harmed: bool | None = None,
    harm_detail: str = "",
    attempts: tuple[str, ...] = (),
) -> CellResult:
    return CellResult(
        key=f"X/{case}/1",
        config_id="X",
        case_id=case,
        domain=domain,
        repeat=1,
        run_id=f"run-{case}",
        status="SUCCEEDED",
        planner_used="llm",
        subtasks=(
            SubtaskGrade(
                subtask_id="pii_check",
                skill="pii.detect",
                status="COMPLETED",
                agent_id=pii_agent,
                passed=pii_passed,
                n=1,
                N=1,
                detail=detail,
            ),
        ),
        tokens_in=1,
        tokens_out=1,
        compute_ms=1.0,
        wall_s=1.0,
        contention_events=0,
        started_at=T0,
        finished_at=T0,
        pool="mixed" if harmed is not None else "clean",
        harmed=harmed,
        harm_detail=harm_detail,
        attempt_agents=attempts,
    )


def test_fill_refuses_unknown_placeholder() -> None:
    assert fill("a {{x}} b {{ y }}", {"x": "1", "y": "2"}) == "a 1 b 2"
    with pytest.raises(RenderError, match="missing_key"):
        fill("{{missing_key}}", {})
    assert render("v={{x}}", {"x": "3"}).endswith("v=3")


def test_readme_block_is_replaced_and_markers_are_required() -> None:
    readme = f"top\n{README_BEGIN}\nold\n{README_END}\nbottom\n"
    out = update_readme(readme, "new {{x}}\n", {"x": "9"})
    assert out == f"top\n{README_BEGIN}\nnew 9\n{README_END}\nbottom\n"
    with pytest.raises(RenderError, match="RESULTS:BEGIN"):
        update_readme("no markers", "x", {})


def test_harm_types_split_output_billing_egress() -> None:
    cells = [
        cell("a", harmed=True, harm_detail=f"grader fail on {POISON}"),
        cell("b", harmed=True, harm_detail="A8c usage misreport not reported"),
        cell(
            "c",
            harmed=True,
            harm_detail=f"grader fail on {POISON}; A8c usage misreport not reported",
        ),
        cell("d", harmed=False, attempts=("exfil-http@vendor-x.local",)),
        cell("e", harmed=False),
    ]
    t = harm_types(cells)
    assert (t["N"], t["harmed"], t["output"], t["billing"], t["both"]) == (5, 3, 2, 2, 1)
    assert (t["egress_cells"], t["egress_harm"]) == (1, 0)


def test_poison_identity_compares_grader_lines_case_by_case() -> None:
    mixed = [
        cell("1", pii_agent=POISON, detail="tp=9 fp=1 fn=1", pii_passed=False),
        cell("2", pii_agent=POISON, detail="tp=5 fp=0 fn=0"),
        cell("3", pii_agent=FAST, detail="tp=5 fp=0 fn=0"),
    ]
    clean = [
        cell("1", detail="tp=9 fp=1 fn=1", pii_passed=False),
        cell("2", detail="tp=4 fp=0 fn=1"),
        cell("3", detail="tp=5 fp=0 fn=0"),
    ]
    same, served, fail_same, fails, honest = poison_identity(mixed, clean)
    assert (same, served, fail_same, fails) == (1, 2, 1, 1)
    assert honest == {"vendor-c-pii-fast"}


def _csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def test_evidence_digest_counts_and_keeps_no_evidence_text(tmp_path: Path) -> None:
    ev = tmp_path / "evidence"
    ev.mkdir()
    _csv(
        ev / "violations.csv",
        ["id", "agent_id", "kind", "severity", "evidence", "at", "rel_epoch"],
        [
            ["1", POISON, "SPOTCHECK_FAIL", "SOFT", "secret-ish 4111111111111111", "t", "0"],
            ["2", POISON, "SPOTCHECK_FAIL", "SOFT", "x", "t", "0"],
        ],
    )
    _csv(
        ev / "ledger_entries.csv",
        ["id", "agent_id", "skill", "kind", "state", "spot_check_passed"],
        [
            ["1", POISON, "pii.detect", "task", "COMPLETED", "t"],
            ["2", POISON, "pii.detect", "canary", "FAILED_SPOTCHECK", "f"],
            ["3", POISON, "pii.detect", "task", "COMPLETED", "t"],
        ],
    )
    measured = {"s": 2, "f": 0, "reliability_lb": 0.3, "latency": 10.0, "cost_norm": 0.5}
    terms = [
        {"agent_id": POISON, "terms": {**measured, "utility": 0.1}},
        {"agent_id": FAST, "terms": {}},
    ]
    _csv(
        ev / "selection_decisions.csv",
        ["id", "skill", "chosen", "reason", "candidates", "at"],
        [
            ["1", "pii.detect", POISON, "exploration", "[]", "2026-11-07 10:00"],
            [
                "2",
                "pii.detect",
                POISON,
                "max utility (measured)",
                json.dumps(terms),
                "2026-11-07 11:00",
            ],
        ],
    )
    d = evidence_digest("seg", ev)
    assert d.available and d.ledger_rows == 3
    assert [(v.kind, v.n) for v in d.violations] == [("SPOTCHECK_FAIL", 2)]
    by = {(r.kind, r.spot_check_passed): r.n for r in d.ledger}
    assert by == {("task", True): 2, ("canary", False): 1}
    assert {(s.reason, s.n) for s in d.selections} == {
        ("exploration", 1),
        ("max utility (measured)", 1),
    }
    last = {t.agent_id: t for t in d.last_pii_selection}
    assert last[POISON].s == 2 and last[POISON].utility == 0.1
    assert last[FAST].reliability_lb is None
    assert "4111111111111111" not in d.model_dump_json()

    missing = evidence_digest("old", tmp_path / "nope")
    assert not missing.available and missing.ledger == ()


def _prov(**kw: object) -> Provenance:
    base: dict[str, object] = {
        "final": True,
        "git_sha": "abc",
        "git_dirty": False,
        "manifest_sha256": "m",
        "model_digests": {"q": "d"},
        "ollama_env": {},
        "generator_version": "1",
        "grader_version": "1",
        "config_sha256": "c",
        "at": T0,
    }
    base.update(kw)
    return Provenance.model_validate(base)


def test_check_provenance_refuses_dirty_or_mixed_runs() -> None:
    assert check_provenance({"a": _prov(), "b": _prov(config_sha256="other")}).git_sha == "abc"
    with pytest.raises(ExportRefused, match="dirty"):
        check_provenance({"a": _prov(git_dirty=True)})
    with pytest.raises(ExportRefused, match="git_sha"):
        check_provenance({"a": _prov(), "b": _prov(git_sha="def")})
    with pytest.raises(ExportRefused, match="model_digests"):
        check_provenance({"a": _prov(), "b": _prov(model_digests={"q": "e"})})


def test_phase5c_logs_parse_without_user_ids(tmp_path: Path) -> None:
    scenario = (
        "  A5a/sole-provider: exposure={e} (real {r}, canaries 2) first_bad={fb} "
        "detect_delay=4 strikes=3 rel_epoch={ep}\n"
        "id   mode           status  actual\n"
        "A5a  sole-provider  caught  SPOTCHECK_FAIL\n"
    )
    (tmp_path / "a5a.log").write_text(scenario.format(e=8, r=6, fb=4, ep=0))
    (tmp_path / "recatch.log").write_text(scenario.format(e=7, r=5, fb=3, ep=1))
    (tmp_path / "approve.log").write_text(
        "reinstate status=active exam=5/5 approved=True by=U0SOMEUSER1 reason=operator approved "
        "request_id=r1 action=approve to_state=ACTIVE\nrel_epoch 0 -> 1 tags=[]\n"
    )
    (tmp_path / "deny.log").write_text(
        "reinstate status=denied exam=5/5 approved=False by=U0SOMEUSER1 reason=operator denied "
        "request_id=r2 action=deny to_state=QUARANTINED\n"
    )
    (tmp_path / "slack-approve.log").write_text(
        "approved=True by=U0SOMEUSER1 reason=operator approved at=2026-11-05T21:47:29Z\n"
    )
    (tmp_path / "slack-timeout.log").write_text(
        "approved=False by=slack reason=timeout at=2026-11-05T21:45:23Z\n"
    )
    p = phase5c_evidence(tmp_path)
    assert (p.catch.exposure, p.catch.first_bad, p.catch.rel_epoch) == (8, 4, 0)
    assert (p.recatch.exposure_real, p.recatch.rel_epoch, p.recatch.status) == (5, 1, "caught")
    assert (p.approve_epoch_from, p.approve_epoch_to, p.deny_to_state) == (0, 1, "QUARANTINED")
    assert p.slack_timeout == "approved=False reason=timeout"
    assert "U0SOMEUSER1" not in p.model_dump_json()
    (tmp_path / "deny.log").unlink()
    with pytest.raises(ExportRefused, match="deny"):
        phase5c_evidence(tmp_path)


def test_committed_results_md_and_readme_match_results_dir() -> None:
    """The CI drift check, as a test: RESULTS.md and the README block are what results/ and
    the templates produce."""
    out = render_all(
        REPO / "results",
        REPO / "docs/results/RESULTS.template.md",
        REPO / "docs/results/README.results.template.md",
        REPO / "README.md",
    )
    assert out.results_md == (REPO / "RESULTS.md").read_text()
    assert out.readme == (REPO / "README.md").read_text()
