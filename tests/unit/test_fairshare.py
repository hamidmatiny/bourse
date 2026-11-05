"""Meter fair share: per-agent in-flight cap, token rate, CONTENTION exclusion."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

import pytest

from bourse.broker.canary import canary_grader_fail, canary_ok
from bourse.broker.reliability import agent_stats
from bourse.contracts.broker import DelegationState, LedgerRow
from bourse.contracts.skills import SkillId
from bourse.meter.fairshare import AgentGate, QueueTimeout, RateLimitUnsatisfiable


def _ledger(
    *,
    state: DelegationState = DelegationState.COMPLETED,
    spot: bool | None = True,
    kind: Literal["task", "canary"] = "canary",
) -> LedgerRow:
    return LedgerRow(
        run_id="run-1",
        subtask_id="pii",
        attempt=1,
        agent_id="a@vendor-a.local",
        skill=SkillId.PII_DETECT,
        revision_digest="0" * 64,
        agent_major=1,
        state=state,
        tokens_in=0,
        tokens_out=0,
        tool_calls=0,
        shadow_usd=0.0,
        price_table_version="t",
        wall_ms=1.0,
        spot_check_passed=spot,
        at=datetime(2026, 11, 2, tzinfo=UTC),
        kind=kind,
    )


async def test_agent_gate_max_one_in_flight() -> None:
    gate = AgentGate(max_in_flight=1, tokens_per_min=1_000_000.0, queue_timeout_s=5.0)
    order: list[str] = []

    async def hold(label: str, hold_s: float) -> None:
        wait = await gate.acquire("agent-a", 1)
        order.append(f"enter-{label}:{wait:.0f}")
        await asyncio.sleep(hold_s)
        gate.release("agent-a")
        order.append(f"exit-{label}")

    await asyncio.gather(hold("first", 0.05), hold("second", 0.01))
    assert order[0].startswith("enter-first")
    assert "exit-first" in order
    assert any(x.startswith("enter-second") for x in order)
    second = next(x for x in order if x.startswith("enter-second"))
    assert float(second.split(":")[1]) >= 40.0


async def test_agent_gate_token_rate_limits() -> None:
    gate = AgentGate(max_in_flight=2, tokens_per_min=6_000.0, queue_timeout_s=5.0)
    t0 = asyncio.get_event_loop().time()
    w1 = await gate.acquire("agent-b", 6_000)
    gate.release("agent-b")
    w2 = await gate.acquire("agent-b", 200)
    gate.release("agent-b")
    elapsed = asyncio.get_event_loop().time() - t0
    assert w1 < 20.0
    assert w2 >= 150.0
    assert elapsed >= 0.15


async def test_request_larger_than_capacity_rejected() -> None:
    gate = AgentGate(max_in_flight=1, tokens_per_min=100.0, queue_timeout_s=1.0)
    with pytest.raises(RateLimitUnsatisfiable, match="exceeds bucket capacity"):
        await gate.acquire("agent-c", 101)


async def test_queue_timeout_raises() -> None:
    gate = AgentGate(max_in_flight=1, tokens_per_min=1_000_000.0, queue_timeout_s=0.05)
    await gate.acquire("agent-d", 1)
    with pytest.raises(QueueTimeout):
        await gate.acquire("agent-d", 1)
    gate.release("agent-d")


async def test_global_parallel_serializes_across_agents() -> None:
    """OLLAMA_NUM_PARALLEL=1: second agent waits; wait counts in queue_wait_ms."""
    gate = AgentGate(
        max_in_flight=1, tokens_per_min=1_000_000.0, queue_timeout_s=5.0, global_parallel=1
    )
    waits: list[float] = []

    async def hold(agent: str, hold_s: float) -> None:
        w = await gate.acquire(agent, 1)
        waits.append(w)
        await asyncio.sleep(hold_s)
        gate.release(agent)

    await asyncio.gather(hold("a", 0.08), hold("b", 0.01))
    assert waits[0] < 20.0
    assert waits[1] >= 50.0


def test_exam_deadline_from_p95() -> None:
    from bourse.trust.exam import ExamConfig

    cfg = ExamConfig(deadline_s=300.0, deadline_by_model={"qwen3.5:27b-q4_K_M": 45.0})
    assert cfg.task_deadline_s("qwen3.5:27b-q4_K_M") == 300.0  # base wins when larger
    cfg2 = ExamConfig(deadline_s=30.0, deadline_by_model={"qwen3.5:27b-q4_K_M": 45.0})
    assert cfg2.task_deadline_s("qwen3.5:27b-q4_K_M") == 45.0
    assert cfg2.task_deadline_s("unknown") == 30.0


def test_meter_config_rejects_undersized_bucket() -> None:
    from bourse.meter.app import MeterConfig, create_meter_app
    from bourse.meter.repo import InMemoryMeterRepository

    with pytest.raises(ValueError, match="tokens_per_min"):
        create_meter_app(
            providers={},
            keys={"a": "k" * 16},
            repo=InMemoryMeterRepository(),
            config=MeterConfig(
                max_tokens_per_call=2048,
                max_prompt_tokens=2048,
                tokens_per_min=100.0,
                queue_timeout_s=10.0,
                deadline_s=120.0,
            ),
        )


def test_canary_grader_fail_only_completed_contract_valid() -> None:
    assert canary_grader_fail(_ledger(state=DelegationState.COMPLETED, spot=False))
    assert not canary_grader_fail(_ledger(state=DelegationState.COMPLETED, spot=True))
    assert canary_ok(_ledger(state=DelegationState.COMPLETED, spot=True))
    for state in (
        DelegationState.TIMEOUT,
        DelegationState.ERROR,
        DelegationState.CONTENTION,
        DelegationState.FAILED_CONTRACT,
        DelegationState.CANCELED_QUARANTINE,
        DelegationState.SESSION_POLICY,
    ):
        assert not canary_grader_fail(_ledger(state=state, spot=False))
        assert not canary_grader_fail(_ledger(state=state, spot=None))


def test_contention_excluded_from_reliability_f() -> None:
    rows = [
        _ledger(state=DelegationState.COMPLETED, spot=True, kind="task"),
        _ledger(state=DelegationState.CONTENTION, spot=True, kind="task"),
        _ledger(state=DelegationState.TIMEOUT, spot=None, kind="task"),
    ]
    st = agent_stats(rows, 0.3)
    assert (st.s, st.f) == (1, 1)
