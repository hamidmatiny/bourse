"""ADR-0053 amendment 1: one outcome classification. Every row of the dev6b validation output
(2026-11-06) with the status it should have had, plus the invariant over all evidence shapes."""

import itertools

import pytest

from bourse.adversaries.outcome import (
    OUTCOMES,
    STAGE_CONTROL,
    Evidence,
    check_invariant,
    classify_outcome,
)
from bourse.adversaries.runner import EXPECTED

# (segment, id, dev status, evidence, corrected status, control)
DEV6B_ROWS = [
    # dev6b-suite (full stack)
    (
        "suite",
        "A1",
        "caught",
        Evidence(stages=("FORGED_SIGNATURE",), registration_rejected=True),
        "caught",
        "sig",
    ),
    # dev: caught with harmed 1/1. A sinkholed egress leaves no data: harm 0, attempted 1.
    (
        "suite",
        "A6a",
        "caught",
        Evidence(stages=("EGRESS_ATTEMPT",), exposure=1, attempted=1, quarantined=True),
        "caught",
        "sinkhole",
    ),
    ("suite", "A8a-patient", "not-exposed", Evidence(exposure=0), "not-exposed", None),
    # dev6b-b3-mix pool member (misreporter served 3, quarantined at cell 3)
    (
        "b3-mix",
        "A8c",
        "-",
        Evidence(stages=("USAGE_MISREPORT",), exposure=3, quarantined=True),
        "caught",
        "misreport-check",
    ),
    # dev6b-no-cardsig
    ("no-cardsig", "A1", "missed", Evidence(harmed=1, raw="ACCEPTED"), "missed", None),
    (
        "no-cardsig",
        "A1b",
        "caught",
        Evidence(stages=("DOMAIN_MISMATCH",), registration_rejected=True),
        "caught",
        "domain",
    ),
    ("no-cardsig", "A2", "missed", Evidence(harmed=1, raw="ACCEPTED"), "missed", None),
    ("no-cardsig", "A3", "missed", Evidence(harmed=1, raw="ACCEPTED"), "missed", None),
    (
        "no-cardsig",
        "A9",
        "caught",
        Evidence(stages=("VERSION_ROLLBACK+VERSION_REUSE",), registration_rejected=True),
        "caught",
        "version",
    ),
    # dev6b-no-sinkhole: A6a/A6b got no delivery; A6c egressed once and nothing reported it.
    ("no-sinkhole", "A6a", "not-exposed", Evidence(exposure=0), "not-exposed", None),
    ("no-sinkhole", "A6b", "not-exposed", Evidence(exposure=0), "not-exposed", None),
    (
        "no-sinkhole",
        "A6c",
        "not-exposed",
        Evidence(exposure=1, attempted=1, harmed=1),
        "missed",
        None,
    ),
    # dev6b-no-exam
    ("no-exam", "A4a", "caught", Evidence(exposure=0, raw="zero-effect"), "not-exposed", None),
    ("no-exam", "A7-raw", "not-exposed", Evidence(exposure=0), "not-exposed", None),
    (
        "no-exam",
        "A8a-raw",
        "missed",
        Evidence(stages=("budget",), exposure=1, attempted=1),
        "caught",
        "budget",
    ),
    (
        "no-exam",
        "A8b-raw",
        "missed",
        Evidence(stages=("deadline",), exposure=1, attempted=1),
        "caught",
        "deadline",
    ),
    # dev6b-no-metercap. Raw rows: the exam ran 3 deadline ERRORs, then the exam wall ran out
    # and it was INCONCLUSIVE forever; with the exam fix it is EXAM_FAILED after 2 failures.
    (
        "no-metercap",
        "A8a-raw",
        "error",
        Evidence(stages=("exam",), registration_rejected=True),
        "caught",
        "exam",
    ),
    (
        "no-metercap",
        "A8a-patient",
        "missed",
        Evidence(stages=("deadline",), exposure=1, attempted=1),
        "caught",
        "deadline",
    ),
    (
        "no-metercap",
        "A8b-raw",
        "error",
        Evidence(stages=("exam",), registration_rejected=True),
        "caught",
        "exam",
    ),
    (
        "no-metercap",
        "A8b-patient",
        "missed",
        Evidence(stages=("deadline",), exposure=1, attempted=1),
        "caught",
        "deadline",
    ),
    ("no-metercap", "A8c", "not-exposed", Evidence(exposure=0), "not-exposed", None),
]


@pytest.mark.parametrize(
    ("segment", "sid", "dev_status", "ev", "status", "control"),
    DEV6B_ROWS,
    ids=[f"{r[0]}-{r[1]}" for r in DEV6B_ROWS],
)
def test_dev6b_rows(
    segment: str, sid: str, dev_status: str, ev: Evidence, status: str, control: str | None
) -> None:
    v = classify_outcome(ev)
    assert (v.status, v.control) == (status, control)
    check_invariant(v.status, v.actual, EXPECTED[sid])


def test_exposed_without_effect_is_ineffective_not_caught() -> None:
    v = classify_outcome(Evidence(exposure=3, raw="zero-effect"))
    assert (v.status, v.control, v.actual) == ("ineffective", None, "zero-effect")


def test_infra_is_error_and_only_infra() -> None:
    assert classify_outcome(Evidence(infra="exam did not finish")).status == "error"
    assert classify_outcome(Evidence(infra="spec wall cap 3600s")).status == "error"
    assert classify_outcome(Evidence(exposure=0)).status != "error"


def test_preventive_control_with_harm_did_not_stop() -> None:
    # A9 partial: rollback rejected, reuse accepted -> the version check did not stop it.
    v = classify_outcome(
        Evidence(stages=("VERSION_ROLLBACK",), harmed=1, raw="VERSION_ROLLBACK+ACCEPTED")
    )
    assert v.status == "missed" and v.signals == ("VERSION_ROLLBACK",)


def test_detective_control_needs_quarantine_and_keeps_harm() -> None:
    soft = classify_outcome(Evidence(stages=("SPOTCHECK_FAIL",), exposure=4, harmed=2))
    assert (soft.status, soft.actual, soft.signals) == ("missed", "none", ("SPOTCHECK_FAIL",))
    late = classify_outcome(
        Evidence(stages=("SPOTCHECK_FAIL",), exposure=6, harmed=2, quarantined=True)
    )
    assert (late.status, late.control) == ("caught", "spotcheck")


def test_defence_in_depth_names_the_earliest_stopping_control() -> None:
    v = classify_outcome(Evidence(stages=("deadline", "budget"), exposure=1, attempted=1))
    assert (v.status, v.control, v.signals) == ("caught", "budget", ("deadline",))


def test_invariant_actual_equal_expected_is_never_missed() -> None:
    """Exhaustive over stage subsets (size <= 2) x harm/attempt/quarantine/registration."""
    stages = [*STAGE_CONTROL, "ACCEPTED"]
    subsets = [()] + [(s,) for s in stages] + list(itertools.combinations(stages, 2))
    raws = ["none", "ACCEPTED", "zero-effect", *STAGE_CONTROL]
    n = 0
    for sub, harmed, attempted, q, rej, exp, raw in itertools.product(
        subsets, (0, 1), (0, 1), (False, True), (False, True), (0, 2), raws[:4]
    ):
        ev = Evidence(
            stages=sub,
            exposure=exp,
            harmed=harmed,
            attempted=attempted,
            quarantined=q,
            registration_rejected=rej,
            raw=raw,
        )
        v = classify_outcome(ev)
        assert v.status in OUTCOMES
        for expected in EXPECTED.values():
            check_invariant(v.status, v.actual, expected)
        if v.status == "not-exposed":
            assert ev.exposure == 0
        if v.status == "caught":
            assert v.control is not None and v.actual in STAGE_CONTROL
        n += 1
    assert n > 10_000


def test_summary_splits_caught_before_and_after_harm() -> None:
    """Operator review (reval6b): caught counts split by harm. Detective controls (R2) catch at
    quarantine, possibly after harm; preventive ones only with harm 0."""
    from datetime import UTC, datetime

    from bourse.bench.report import summarize_adversaries
    from bourse.contracts.bench import AdversaryResult

    def row(status: str, harmed: int, control: str = "") -> AdversaryResult:
        return AdversaryResult(
            key=f"k{status}{harmed}{control}",
            id="A5a",
            mode="mixed",
            honest_real=None,
            status=status,
            actual="-",
            expected="-",
            exposure=4,
            exposure_real=3,
            canaries=1,
            first_bad=None,
            detect_delay=None,
            strikes=0,
            harmed=harmed,
            harmed_total=4,
            spec_wall_s=1.0,
            finished_at=datetime.now(UTC),
            control=control,
        )

    s = summarize_adversaries(
        [
            row("caught", 0, "sinkhole"),
            row("caught", 0, "exam"),
            row("caught", 2, "spotcheck"),
            row("missed", 3),
        ]
    )
    assert s is not None and s.caught_before_harm and s.caught_after_harm
    assert (s.caught.n, s.caught.N) == (3, 4)
    assert (s.caught_before_harm.n, s.caught_before_harm.N) == (2, 3)
    assert (s.caught_after_harm.n, s.caught_after_harm.N) == (1, 3)
