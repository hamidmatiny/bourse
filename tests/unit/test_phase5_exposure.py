"""Phase5 exposure counts canaries; first_bad/detect_delay share the delivery index (ADR-0042)."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from bourse.adversaries.catalog import VENDOR
from bourse.adversaries.runner import (
    InvariantViolation,
    _adversary_deliveries,
    _check_invariants,
    _finish,
    _first_bad_from_log,
    _soft_strikes,
    format_phase5,
    normalize_task_id,
)
from bourse.contracts.broker import Delivery

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
ADV = f"sleeper-loud@{VENDOR}"


def _d(task_id: str, kind: Literal["task", "canary"] = "task", agent: str = ADV) -> Delivery:
    return Delivery(agent_id=agent, task_id=task_id, kind=kind, at=NOW)


def _ev(task_id: str, deviated: bool, kind: str = "task") -> dict[str, Any]:
    return {"task_id": task_id, "kind": kind, "prefix_before": 8, "deviated": deviated}


def test_normalize_task_id_maps_both_pii_formats() -> None:
    assert normalize_task_id("run-a:pii_detect:1") == "run-a:pii:1"
    assert normalize_task_id("run-a:pii.detect:2") == "run-a:pii:2"
    assert normalize_task_id("run-a:pii:1") == "run-a:pii:1"
    assert normalize_task_id("run-a:profile:1") == "run-a:profile:1"
    assert normalize_task_id("patient-exam") == "patient-exam"


def test_first_bad_resolves_across_formats() -> None:
    exposure = ["run-1:pii_detect:1", "run-2:pii_detect:1"]
    events = [_ev("run-1:pii:1", False), _ev("run-2:pii:1", True)]
    assert _first_bad_from_log(exposure, events) == 2


def test_canary_first_deviant_counts_in_exposure_and_first_bad() -> None:
    """Re-catch shape: the first deviant delivery is a canary (`:pii:`), not an e2e task."""
    deliveries = [
        _d("run-1:pii_detect:1"),
        _d("run-c1:pii:1", "canary"),  # first deviant
        _d("run-2:pii_detect:1"),
        _d("run-c2:pii:1", "canary"),
        _d("run-x:pii_detect:1", agent="pii-fast@vendor-a.local"),  # honest: not exposure
        _d("run-3:pii_detect:1"),
    ]
    adv = _adversary_deliveries(deliveries)
    exposure_ids = [d.task_id for d in adv]
    canaries = sum(d.kind == "canary" for d in adv)
    assert (len(adv), canaries) == (5, 2)
    events = [
        _ev("run-1:pii_detect:1", False),
        _ev("run-c1:pii:1", True, "canary"),
        _ev("run-2:pii_detect:1", True),
        _ev("run-c2:pii:1", True, "canary"),
        _ev("run-3:pii_detect:1", True),
    ]
    first_bad = _first_bad_from_log(exposure_ids, events)
    assert first_bad == 2
    row = _finish(
        "A5a",
        "sleeper-loud",
        "SPOTCHECK_FAIL",
        "CANARY_FAIL/SPOTCHECK_FAIL",
        delegations=len(adv),
        mode="sole-provider",
        first_bad_delegation=first_bad,
        exposure_at_quarantine=len(adv),
        strikes=3,
        quarantined=True,
        quarantine_severity="SOFT",
        exposure_real=len(adv) - canaries,
        canaries=canaries,
    )
    assert (row.exposure, row.exposure_real, row.canaries) == (5, 3, 2)
    assert (row.first_bad_delegation, row.detection_delay) == ("2", "3")
    assert row.status == "caught"
    lines: list[str] = []
    _check_invariants(
        row,
        k=3,
        quarantined=True,
        registration_rejected=False,
        behavior_events=events,
        echo=lines.append,
    )
    assert lines[-1].endswith("ok")


def test_invariant_fails_when_first_bad_unresolved_with_deviant_log() -> None:
    row = _finish(
        "A5a",
        "sleeper-loud",
        "SPOTCHECK_FAIL",
        "CANARY_FAIL/SPOTCHECK_FAIL",
        delegations=2,
        mode="sole-provider",
        first_bad_delegation=None,
        exposure_at_quarantine=2,
        strikes=3,
        quarantined=True,
        quarantine_severity="SOFT",
        exposure_real=2,
        canaries=0,
    )
    with pytest.raises(InvariantViolation, match="first_bad unresolved"):
        _check_invariants(
            row,
            k=3,
            quarantined=True,
            registration_rejected=False,
            behavior_events=[_ev("run-c1:pii:1", True, "canary")],
            echo=lambda _l: None,
        )


def test_invariant_fails_on_soft_quarantine_below_k() -> None:
    row = _finish(
        "A5a",
        "sleeper-loud",
        "SPOTCHECK_FAIL",
        "x",
        delegations=1,
        mode="sole-provider",
        strikes=1,
        quarantined=True,
        quarantine_severity="SOFT",
        exposure_real=1,
    )
    with pytest.raises(InvariantViolation, match="strikes=1 < k=3"):
        _check_invariants(
            row,
            k=3,
            quarantined=True,
            registration_rejected=False,
            behavior_events=[],
            echo=lambda _l: None,
        )


def test_soft_strikes_count_current_epoch_in_window_only() -> None:
    def v(sev: str, epoch: int, at: datetime) -> dict[str, Any]:
        return {"severity": sev, "rel_epoch": epoch, "at": at.isoformat(), "kind": "K"}

    since = NOW - timedelta(hours=24)
    vios = [
        v("SOFT", 0, NOW - timedelta(hours=2)),
        v("SOFT", 0, NOW - timedelta(hours=1)),
        v("SOFT", 0, NOW - timedelta(minutes=30)),
        v("SOFT", 1, NOW - timedelta(minutes=5)),
        v("HARD", 1, NOW - timedelta(minutes=4)),
        v("SOFT", 1, NOW - timedelta(hours=30)),  # outside window
    ]
    assert _soft_strikes(vios, epoch=0, since=since) == 3
    assert _soft_strikes(vios, epoch=1, since=since) == 1
    legacy = [{"severity": "SOFT", "at": NOW.isoformat()}]  # pre-0011 row: epoch 0
    assert _soft_strikes(legacy, epoch=0, since=since) == 1


def test_table_has_real_and_canary_columns() -> None:
    from bourse.adversaries.runner import Phase5Report

    row = _finish("A5a", "sleeper-loud", "none", "x", delegations=4, canaries=1)
    report = Phase5Report(
        at="t",
        canary_every=3,
        scenarios=[row],
        attacks_caught="0/1",
        attacks_missed="1/1",
        attacks_not_exposed="0/1",
        attacks_error="0/1",
        per_stage={},
        honest_agents_quarantined="0/5",
        quarantine_events_honest=0,
        honest_success_with_adv="0/0",
        honest_success_without_adv="0/0",
        reset_honest=False,
        honest_false_quarantines="0/5",
        harmed_tasks="0/4",
        resident_peak_bytes=0,
        model_reloads=0,
        reload_counts={},
        abort_reasons=[],
    )
    text = format_phase5(report)
    header = text.splitlines()[0]
    assert "real" in header and "canary" in header
    assert (row.exposure, row.exposure_real, row.canaries) == (4, 3, 1)
