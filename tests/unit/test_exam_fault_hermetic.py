"""Hermetic: exam attribution from real meter evidence (ADR-0049 amendment 2).

In process: the real meter app over ASGI (fake Ollama upstream), the real ExamRunner with the
real MeterBudgetClient and usage lookup, a registered PENDING agent in the broker test bed.
The agent side is scripted by replacing the exam's send_task:

- slow-loris: accepts the task and never answers, makes no LLM call      -> EXAM_FAILED (A4)
- token burner under no-metercap: loops LLM calls through the meter with the
  exam task id; nothing refuses it                                        -> EXAM_FAILED (A3)
- token burner under the hard cap: the meter refuses it                   -> EXAM_FAILED (A1)
- honest agent whose first LLM call gets an upstream 503 (meter records a 5xx), then answers
  correctly                                                               -> INFRA retry, passed
- honest agent whose LLM calls always get the 5xx                         -> INCONCLUSIVE, never
                                                                             EXAM_FAILED
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from bourse.broker.delegate import MeterBudgetClient
from bourse.contracts.trust import TrustState
from bourse.llm.base import Provider
from bourse.llm.ollama import OllamaProvider
from bourse.meter.app import MeterConfig, create_meter_app
from bourse.meter.keys import parse_keys
from bourse.meter.repo import InMemoryMeterRepository
from bourse.runtime.breaker import CircuitBreaker
from bourse.stack.smoke import CallResult, send_task
from bourse.trust import exam as exam_mod
from bourse.trust.exam import (
    EXAM_FAILED,
    EXAM_INCONCLUSIVE,
    INFRA,
    ExamConfig,
    ExamResult,
    ExamRunner,
)
from bourse.trust.honeytokens import InMemoryHoneytokenRepository
from tests.broker_harness import BrokerBed, honest
from tests.unit.broker.test_exam_canary import SECRET, Clock, pii_agent
from tests.unit.fakes import OLLAMA, OLLAMA_CHAT

MODEL = "qwen3.5:27b-q4_K_M"
METER = "http://meter.test"
BROKER_KEY = secrets.token_hex(16)
AGENT_KEY = secrets.token_hex(16)
CFG = ExamConfig(
    k=5,
    pass_n=4,
    deadline_s=0.3,
    exam_timeout_s=30.0,
    infra_max_retries=2,
    infra_backoff_s=0.0,
    slot_grace_s=0.05,
)


class Upstream:
    """Fake Ollama: answers after a short delay; `fail_first` calls return 503."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.calls = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OLLAMA_CHAT
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.calls <= self.fail_first:
            return httpx.Response(503, text="model loading")
        return httpx.Response(
            200,
            json={
                "model": MODEL,
                "message": {"role": "assistant", "content": "x"},
                "done": True,
                "prompt_eval_count": 60,
                "eval_count": 256,
            },
        )


class Meter:
    def __init__(self, upstream: Upstream, *, metercap: bool) -> None:
        self.repo = InMemoryMeterRepository()
        up = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        providers: dict[str, list[Provider]] = {MODEL: [OllamaProvider(OLLAMA, MODEL, up)]}
        app = create_meter_app(
            providers=providers,
            keys=parse_keys(f"broker:{BROKER_KEY},x-pii:{AGENT_KEY}"),
            repo=self.repo,
            breaker=CircuitBreaker(),
            config=MeterConfig(
                max_tokens_per_call=256,
                deadline_s=5.0,
                require_task_budget=metercap,
                stack_profile="full" if metercap else "no-metercap",
            ),
        )
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=METER)
        self.calls: set[asyncio.Task[httpx.Response]] = set()

    async def llm_call(self, task_id: str) -> httpx.Response:
        """The meter is its own process in the stack: a request keeps running server-side when
        the agent's task is cancelled. Model that with an independent task (in process, the
        cancel would otherwise reach into the meter's gate)."""
        call = asyncio.create_task(
            self.http.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": [{"role": "user", "content": "x" * 200}]},
                headers={"Authorization": f"Bearer {AGENT_KEY}", "X-Bourse-Task-Id": task_id},
            )
        )
        self.calls.add(call)
        call.add_done_callback(self.calls.discard)
        return await asyncio.shield(call)

    async def drain(self) -> None:
        if self.calls:
            await asyncio.wait(set(self.calls), timeout=10)


@pytest.fixture
async def bed() -> AsyncIterator[BrokerBed]:
    b = await BrokerBed([pii_agent(honest)], activate=False).setup()
    yield b
    await b.aclose()


async def _exam(bed: BrokerBed, meter: Meter, lines: list[str]) -> ExamResult:
    runner = ExamRunner(
        bed.registry_service.repo,
        bed.http,
        InMemoryHoneytokenRepository(),
        SECRET,
        CFG,
        clock=Clock(),
        budgets=MeterBudgetClient(meter.http, METER, BROKER_KEY),
        task_max_tokens=2048,
        task_max_calls=3,
        usage_lookup=lambda task_id: meter.repo.list_usage(task_id=task_id),
        echo=lines.append,
    )
    agent_id = pii_agent(honest).agent_id
    async with meter.http:
        async with asyncio.timeout(20):
            result = await runner.run(agent_id)
        await meter.drain()
    assert result is not None
    return result


async def _kinds(bed: BrokerBed) -> list[str]:
    return [
        v.kind for v in await bed.registry_service.repo.list_violations(pii_agent(honest).agent_id)
    ]


async def test_slow_loris_gets_exam_failed(bed: BrokerBed, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_loris(*_a: Any, **_k: Any) -> CallResult:
        await asyncio.sleep(3600)
        raise AssertionError

    monkeypatch.setattr(exam_mod, "send_task", slow_loris)
    lines: list[str] = []
    result = await _exam(bed, Meter(Upstream(), metercap=True), lines)
    assert not result.passed and not result.inconclusive
    assert [(t.outcome, t.fault, t.fault_rule) for t in result.tasks] == [
        ("ERROR", "agent", "A4"),
        ("ERROR", "agent", "A4"),
    ]
    assert all(t.infra_retries == 0 for t in result.tasks)
    assert "in_flight=0" in result.tasks[0].evidence and "calls=0/3" in result.tasks[0].evidence
    assert await _kinds(bed) == [EXAM_FAILED]
    assert any("fault=agent rule=A4" in ln for ln in lines)


async def test_token_burner_without_metercap_gets_exam_failed(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = Meter(Upstream(), metercap=False)

    async def burner(_card: Any, _skill: str, _p: Any, task_id: str, *_a: Any, **_k: Any) -> Any:
        while True:
            resp = await meter.llm_call(task_id)
            assert resp.status_code == 200  # no-metercap: nothing refuses it

    monkeypatch.setattr(exam_mod, "send_task", burner)
    result = await _exam(bed, meter, [])
    assert not result.passed and not result.inconclusive
    assert [(t.outcome, t.fault, t.fault_rule) for t in result.tasks] == [
        ("ERROR", "agent", "A3"),
        ("ERROR", "agent", "A3"),
    ]
    assert "calls beyond task budget" in result.tasks[0].evidence
    assert await _kinds(bed) == [EXAM_FAILED]


async def test_token_burner_refused_by_the_hard_cap_gets_exam_failed(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = Meter(Upstream(), metercap=True)

    async def burner(_card: Any, _skill: str, _p: Any, task_id: str, *_a: Any, **_k: Any) -> Any:
        while True:
            resp = await meter.llm_call(task_id)
            if resp.status_code == 429:
                code = resp.json()["error"]["code"]
                return CallResult(
                    state="TASK_STATE_FAILED",
                    output=None,
                    error=code,
                    wall_s=0.0,
                    error_kind="token_budget",
                )

    monkeypatch.setattr(exam_mod, "send_task", burner)
    result = await _exam(bed, meter, [])
    assert not result.passed and not result.inconclusive
    assert [(t.outcome, t.fault, t.fault_rule) for t in result.tasks] == [
        ("ERROR", "agent", "A1"),
        ("ERROR", "agent", "A1"),
    ]
    assert "meter refused" in result.tasks[0].evidence
    assert await _kinds(bed) == [EXAM_FAILED]


def _honest_via_meter(meter: Meter, real: Any) -> Any:
    """One LLM call through the meter per attempt; on a 5xx report llm_unavailable (as the
    agents' failure_kind does), else answer with the honest fake agent."""

    async def send(card: Any, skill: str, payload: Any, task_id: str, *a: Any, **k: Any) -> Any:
        resp = await meter.llm_call(task_id)
        if resp.status_code >= 500:
            return CallResult(
                state="TASK_STATE_FAILED",
                output=None,
                error=f"HTTP {resp.status_code}",
                wall_s=0.0,
                error_kind="llm_unavailable",
            )
        return await real(card, skill, payload, task_id, *a, **k)

    return send


async def test_honest_agent_behind_meter_5xx_is_retried_not_failed(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = Meter(Upstream(fail_first=1), metercap=True)
    monkeypatch.setattr(exam_mod, "send_task", _honest_via_meter(meter, send_task))
    lines: list[str] = []
    result = await _exam(bed, meter, lines)
    assert result.passed and not result.inconclusive
    assert [t.outcome for t in result.tasks] == ["passed"] * 5
    assert result.tasks[0].infra_retries == 1
    assert EXAM_FAILED not in await _kinds(bed)
    infra_line = next(ln for ln in lines if "outcome=INFRA" in ln)
    assert "fault=infra rule=I1" in infra_line and "meter 502 upstream_failed" in infra_line
    rec = await bed.registry_service.repo.get_agent(pii_agent(honest).agent_id)
    assert rec is not None and rec.state is TrustState.ACTIVE


async def test_honest_agent_behind_persistent_meter_5xx_is_inconclusive(
    bed: BrokerBed, monkeypatch: pytest.MonkeyPatch
) -> None:
    meter = Meter(Upstream(fail_first=10_000), metercap=True)
    monkeypatch.setattr(exam_mod, "send_task", _honest_via_meter(meter, send_task))
    result = await _exam(bed, meter, [])
    assert result.inconclusive and not result.passed
    assert all(t.outcome == INFRA and t.fault == "infra" for t in result.tasks)
    assert result.tasks[0].infra_retries == CFG.infra_max_retries
    kinds = await _kinds(bed)
    assert kinds == [EXAM_INCONCLUSIVE] and EXAM_FAILED not in kinds
