"""Hard deadlines on every exam task and A2A send/await (ADR-0049 amendment).

A hung agent, meter or cancel must end as an INFRA outcome (retry, then INCONCLUSIVE), never
a hang. Progress is visible while an exam runs.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from bourse.contracts.exam import ExamProgress
from bourse.contracts.metering import TaskBudget
from bourse.contracts.trust import TrustState
from bourse.stack.activation import exam_progress_lines, exam_wait_budget_s
from bourse.trust import exam as exam_mod
from bourse.trust.exam import (
    EXAM_FAILED,
    EXAM_INCONCLUSIVE,
    EXAM_TIMEOUT,
    INFRA,
    ExamConfig,
    ExamRunner,
    ExamScheduler,
)
from bourse.trust.honeytokens import InMemoryHoneytokenRepository
from tests.broker_harness import BrokerBed, honest
from tests.unit.broker.test_exam_canary import SECRET, Clock, pii_agent

FAST = ExamConfig(
    k=1,
    pass_n=1,
    cooldown_s=600,
    deadline_s=0.1,
    exam_timeout_s=30.0,
    infra_max_retries=2,
    infra_backoff_s=0.0,
    slot_grace_s=0.05,
)


@pytest.fixture
async def bed() -> AsyncIterator[BrokerBed]:
    b = await BrokerBed([pii_agent(honest)], activate=False).setup()
    yield b
    await b.aclose()


def _runner(bed: BrokerBed, cfg: ExamConfig = FAST, **kw: Any) -> ExamRunner:
    return ExamRunner(
        bed.registry_service.repo,
        bed.http,
        InMemoryHoneytokenRepository(),
        SECRET,
        cfg,
        clock=Clock(),
        **kw,
    )


async def _hang(*_a: Any, **_k: Any) -> Any:
    await asyncio.sleep(3600)


async def test_hung_send_is_infra_retried_then_inconclusive(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def hang(*a: Any, **k: Any) -> Any:
        calls.append(str(a[3]))
        await asyncio.sleep(3600)

    monkeypatch.setattr(exam_mod, "send_task", hang)
    lines: list[str] = []
    agent_id = pii_agent(honest).agent_id
    started = time.monotonic()
    result = await _runner(bed, echo=lines.append).run(agent_id)
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    assert result is not None and result.inconclusive and not result.passed
    assert len(calls) == 3  # 1 + 2 INFRA retries
    assert [t.outcome for t in result.tasks] == [INFRA]
    assert result.tasks[0].infra_retries == 2
    assert EXAM_TIMEOUT in result.tasks[0].detail
    kinds = [v.kind for v in await bed.registry_service.repo.list_violations(agent_id)]
    assert kinds == [EXAM_INCONCLUSIVE]
    assert EXAM_FAILED not in kinds
    rec = await bed.registry_service.repo.get_agent(agent_id)
    assert rec is not None and rec.state is TrustState.PENDING
    # Per-task lines reach stdout (diagnostics greps "exam task").
    assert sum("exam task" in ln and "outcome=INFRA" in ln for ln in lines) == 3
    assert any(ln.startswith("exam done") for ln in lines)


class _Budgets:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def register(self, budget: TaskBudget) -> None:
        if self.mode == "hang":
            await asyncio.sleep(3600)
        req = httpx.Request("POST", "http://meter/budgets")
        raise httpx.ConnectError("meter down", request=req)


@pytest.mark.parametrize("mode", ["hang", "error"])
async def test_meter_budget_register_hang_or_error_is_infra(bed: BrokerBed, mode: str) -> None:
    agent_id = pii_agent(honest).agent_id
    runner = _runner(bed)
    runner.budgets = _Budgets(mode)  # type: ignore[assignment]  # duck-typed MeterBudgetClient
    started = time.monotonic()
    result = await runner.run(agent_id)
    assert time.monotonic() - started < 3.0
    assert result is not None and result.inconclusive
    assert result.tasks[0].outcome == INFRA
    expected = EXAM_TIMEOUT if mode == "hang" else "meter: budget register ConnectError"
    assert expected in result.tasks[0].detail


async def test_progress_shows_running_task_and_queued_exam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a1 = pii_agent(honest)
    from tests.broker_harness import FakeAgent
    from tests.registry_helpers import make_party

    a2 = FakeAgent("y-pii", make_party("vendor-y.local"), a1.skills, honest)
    bed = await BrokerBed([a1, a2], activate=False).setup()
    gate = asyncio.Event()

    async def blocked(*a: Any, **k: Any) -> Any:
        await gate.wait()
        await asyncio.sleep(3600)

    monkeypatch.setattr(exam_mod, "send_task", blocked)
    cfg = ExamConfig(
        k=2, pass_n=1, deadline_s=0.3, infra_max_retries=0, infra_backoff_s=0, slot_grace_s=0
    )
    sched = ExamScheduler(_runner(bed, cfg), max_concurrent=1, echo=lambda _m: None)
    try:
        sched.schedule(a1.agent_id)
        sched.schedule(a2.agent_id)
        await asyncio.sleep(0.05)
        prog = sched.progress()
        running = [p for p in prog if p.status == "running"]
        queued = [p for p in prog if p.status == "queued"]
        assert len(running) == 1 and len(queued) == 1
        r = running[0]
        assert (r.skill, r.task_n, r.task_total, r.attempt) == ("pii.detect", 1, 2, 1)
        assert r.task_deadline_s == pytest.approx(0.3)
        lines = exam_progress_lines(prog)
        assert "task 1/2 attempt 1" in lines[0]
        assert lines[-1].startswith("  exam queued:")
        gate.set()
        await asyncio.wait_for(sched.wait(), timeout=10)
        assert sched.progress() == []
        assert all(r is not None and r.inconclusive for r in sched.results.values())
    finally:
        await bed.aclose()


async def test_scheduler_hard_cap_records_inconclusive(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backstop: a stall outside the A2A send (e.g. token store) still ends the exam."""
    agent_id = pii_agent(honest).agent_id
    runner = _runner(bed, ExamConfig(k=1, pass_n=1, deadline_s=0.1, hard_cap_slack_s=0.0))
    monkeypatch.setattr(runner, "hard_cap_s", lambda _aid: 0.2)
    monkeypatch.setattr(runner.tokens, "add_token", _hang)
    sched = ExamScheduler(runner, max_concurrent=1, reschedule_delay_s=3600, echo=lambda _m: None)
    sched.schedule(agent_id)
    await asyncio.wait_for(sched.wait(), timeout=5)
    result = sched.results[agent_id]
    assert result is not None and result.inconclusive
    kinds = [v.kind for v in await bed.registry_service.repo.list_violations(agent_id)]
    assert kinds == [EXAM_INCONCLUSIVE]
    assert sched.progress() == []


async def test_send_task_has_internal_wall_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from bourse.stack import smoke

    monkeypatch.setattr(smoke, "_send_task", _hang)
    monkeypatch.setattr(smoke, "SEND_CAP_GRACE_S", 0.05)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await smoke.send_task("http://agent", "pii.detect", {}, "t:pii:1", deadline_s=0.05)
    assert time.monotonic() - started < 2.0


async def test_inflight_cancel_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from bourse.broker import inflight

    monkeypatch.setattr(inflight, "CANCEL_DEADLINE_S", 0.05)
    reg = inflight.InFlight(cancel=_hang)
    # card/http are never touched: the injected cancel hangs before using them.
    handle = inflight.InFlightHandle("a@x", None, "t1", None)  # type: ignore[arg-type]
    reg.add(handle)
    started = time.monotonic()
    assert await reg.cancel_agent("a@x") == 0
    assert time.monotonic() - started < 2.0


def test_wait_budget_from_task_counts() -> None:
    # 4 agents x 5 tasks + profiler 10 tasks, deadline 300s, 2 retries, concurrency 2.
    budget = exam_wait_budget_s(
        [10, 5, 5, 5, 5],
        task_deadline_s=300.0,
        exam_timeout_s=900.0,
        infra_retries=2,
        concurrency=2,
    )
    cap = 900.0 + 300.0 + 60.0  # every exam hits the hard cap
    assert budget == pytest.approx(5 * cap / 2 + cap + 120.0)
    # Few short tasks: bounded by task count, not by exam_timeout_s.
    small = exam_wait_budget_s(
        [1], task_deadline_s=10.0, exam_timeout_s=900.0, infra_retries=0, concurrency=2
    )
    assert small == pytest.approx(15.0 / 2 + 15.0 + 120.0)
    assert (
        exam_wait_budget_s([], task_deadline_s=1, exam_timeout_s=1, infra_retries=0, concurrency=1)
        == 120.0
    )


def test_progress_lines_format() -> None:
    lines = exam_progress_lines(
        [
            ExamProgress(
                agent_id="vendor-a-reporter@vendor-a.local",
                mode="admission",
                status="running",
                skill="report.compose",
                task_n=3,
                task_total=5,
                attempt=1,
                exam_elapsed_s=250.0,
                task_elapsed_s=84.0,
                task_deadline_s=300.0,
            ),
            ExamProgress(agent_id="b@x", mode="admission", status="queued"),
        ]
    )
    assert lines == [
        "  exam vendor-a-reporter@vendor-a.local report.compose task 3/5 attempt 1 "
        "task_elapsed 84s/300s exam_elapsed 250s (admission)",
        "  exam queued: b@x",
    ]


async def test_exam_that_cannot_pass_is_failed_not_inconclusive(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dev6b no-metercap: A8a-raw/A8b-raw hit their own task deadline on every exam task
    (agent-attributable ERROR), the 900 s exam wall then ran out (INFRA) and the exam was
    INCONCLUSIVE and rescheduled forever. With k=5, pass_n=4 a second failure already makes
    passing impossible: the exam stops there and is EXAM_FAILED (ADR-0049 amendment, 6b)."""
    from bourse.stack.smoke import CallResult

    calls: list[str] = []

    async def deadline(*a: Any, **k: Any) -> CallResult:
        calls.append(str(a[3]))
        return CallResult(
            state="TASK_STATE_FAILED",
            output=None,
            error="deadline 300s",
            wall_s=0.01,
            error_kind="deadline",
        )

    monkeypatch.setattr(exam_mod, "send_task", deadline)
    cfg = ExamConfig(
        k=5, pass_n=4, deadline_s=0.1, exam_timeout_s=30.0, infra_backoff_s=0.0, slot_grace_s=0.05
    )
    agent_id = pii_agent(honest).agent_id
    lines: list[str] = []
    result = await _runner(bed, cfg, echo=lines.append).run(agent_id)
    assert result is not None and not result.passed and not result.inconclusive
    assert len(calls) == 2  # stopped after the 2nd failure (k - pass_n = 1 allowed)
    assert [t.outcome for t in result.tasks] == ["ERROR", "ERROR"]
    assert (result.n, result.total) == (0, 5)
    kinds = [v.kind for v in await bed.registry_service.repo.list_violations(agent_id)]
    assert kinds == [EXAM_FAILED]
    assert any("exam cannot pass, stopping" in ln for ln in lines)


async def test_infra_with_too_few_failures_stays_inconclusive(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failure (still allowed) plus INFRA: passing is still possible -> INCONCLUSIVE."""
    from bourse.stack.smoke import CallResult

    n = 0

    async def first_fails_then_hangs(*a: Any, **k: Any) -> CallResult:
        nonlocal n
        n += 1
        if n == 1:
            return CallResult(
                state="TASK_STATE_FAILED", output=None, error="x", wall_s=0.0, error_kind="deadline"
            )
        await asyncio.sleep(3600)
        raise AssertionError

    monkeypatch.setattr(exam_mod, "send_task", first_fails_then_hangs)
    cfg = ExamConfig(
        k=2,
        pass_n=1,
        deadline_s=0.1,
        exam_timeout_s=30.0,
        infra_max_retries=0,
        infra_backoff_s=0.0,
        slot_grace_s=0.05,
    )
    result = await _runner(bed, cfg).run(pii_agent(honest).agent_id)
    assert result is not None and result.inconclusive and not result.passed
