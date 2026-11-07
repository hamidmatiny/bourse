"""Hermetic: the real A8a token-burner executor against the real meter app (ADR-0041 hard cap).

Everything is in process: the burner's A2A app over ASGI, the meter over ASGI as the agent's
HTTP client, and a fake Ollama upstream that reports 60 prompt tokens and as many output tokens
as were asked for. The task ceiling is set so the pre-6b rule (used + max_tokens <= cap) admits
one more call than the hard cap does, and that call would end above the ceiling.
"""

import json
import secrets

import httpx
import respx

from bourse.adversaries.behaviors import AdversaryExecutor
from bourse.adversaries.catalog import BY_ID, adversary_specs
from bourse.agents.common.app import build_agent_app
from bourse.agents.common.card import signed_card
from bourse.agents.common.spec import AgentRuntime
from bourse.contracts.skills_io import DatasetRef, SchemaProfileIn
from bourse.llm.base import ChatMessage, Provider
from bourse.llm.ollama import OllamaProvider
from bourse.meter.app import BUDGET_EXHAUSTED, MeterConfig, create_meter_app
from bourse.meter.estimate import estimate_prompt_tokens
from bourse.meter.keys import parse_keys
from bourse.meter.repo import InMemoryMeterRepository
from bourse.runtime.breaker import CircuitBreaker
from bourse.stack.smoke import send_task
from tests.registry_helpers import make_party
from tests.unit.fakes import OLLAMA, OLLAMA_CHAT

MODEL = "qwen3.5:27b-q4_K_M"
METER = "http://meter.test"
AGENT = "http://burner.test"
PROMPT_TOKENS = 60


def _upstream(request: httpx.Request) -> httpx.Response:
    asked = json.loads(request.content)["options"]["num_predict"]
    return httpx.Response(
        200,
        json={
            "model": MODEL,
            "message": {"role": "assistant", "content": "x"},
            "done": True,
            "prompt_eval_count": PROMPT_TOKENS,
            "eval_count": asked,
        },
    )


async def test_token_burner_stops_at_the_hard_cap_never_above_it() -> None:
    adv = BY_ID["A8a-raw"]
    spec = adversary_specs(MODEL)[adv.name]
    broker_key, agent_key = secrets.token_hex(16), secrets.token_hex(16)
    router = respx.Router(assert_all_called=False)
    router.post(OLLAMA_CHAT).mock(side_effect=_upstream)
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))
    providers: dict[str, list[Provider]] = {MODEL: [OllamaProvider(OLLAMA, MODEL, upstream)]}
    repo = InMemoryMeterRepository()
    meter = create_meter_app(
        providers=providers,
        keys=parse_keys(f"broker:{broker_key},{adv.name}:{agent_key}"),
        repo=repo,
        breaker=CircuitBreaker(),
        config=MeterConfig(max_tokens_per_call=2048, deadline_s=10.0, require_task_budget=True),
    )
    meter_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=meter), base_url=METER)

    # The burner's request: one user message of 200 "x", max_tokens = spec cap.
    est = estimate_prompt_tokens([ChatMessage(role="user", content="x" * 200)], [])
    per_call = PROMPT_TOKENS + spec.max_tokens_per_call
    assert est >= PROMPT_TOKENS
    calls_ok = 6
    # Soft rule admits call 7 (used + max_tokens <= cap); the hard cap refuses it.
    cap = calls_ok * per_call + spec.max_tokens_per_call + (PROMPT_TOKENS // 2)
    assert calls_ok * per_call + spec.max_tokens_per_call <= cap  # old rule: admit
    assert calls_ok * per_call + est + spec.max_tokens_per_call > cap  # hard cap: refuse
    assert (calls_ok + 1) * per_call > cap  # that call would have ended above the ceiling

    task_id = "run-a8:schema_prof:1"
    async with meter_http:
        put = await meter_http.post(
            "/budgets",
            json={"task_id": task_id, "max_tokens": cap, "max_calls": 50},
            headers={"Authorization": f"Bearer {broker_key}"},
        )
        assert put.status_code == 201
        runtime = AgentRuntime(meter_url=METER, meter_key=agent_key, http_client=meter_http)
        executor = AdversaryExecutor(spec, runtime, adv.behavior)
        card = signed_card(spec, AGENT, make_party(spec.vendor_domain).key)
        agent_app = build_agent_app(executor, card)
        payload = SchemaProfileIn(dataset=DatasetRef.from_csv("a,b\n1,2\n"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app), base_url=AGENT, timeout=30
        ) as http:
            result = await send_task(
                AGENT, "schema.profile", payload.model_dump(mode="json"), task_id, 20.0, http
            )

    rows = await repo.list_usage(task_id=task_id)
    ok = [r for r in rows if r.status == 200]
    refused = [r for r in rows if r.status == 429]
    used = sum(r.tokens_in + r.tokens_out for r in ok)
    assert "FAILED" in result.state
    assert BUDGET_EXHAUSTED in result.error
    assert len(ok) == calls_ok
    assert used == calls_ok * per_call <= cap
    assert len(refused) == 1 and refused[0].error_kind == "BUDGET"
    assert refused[0].prompt_tokens_est == est
