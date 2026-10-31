"""Metering proxy against the host Ollama. Run with `uv run pytest -m live`."""

import json
import secrets

import httpx
import pytest

from bourse.config import get_settings
from bourse.llm.base import Provider
from bourse.llm.ollama import DEFAULT_HTTP_TIMEOUT, OllamaProvider
from bourse.meter.app import MeterConfig, create_meter_app
from bourse.meter.keys import parse_keys
from bourse.meter.repo import InMemoryMeterRepository
from bourse.runtime.breaker import CircuitBreaker

pytestmark = pytest.mark.live

SETTINGS = get_settings()
MODELS = [SETTINGS.bourse_model_large, SETTINGS.bourse_model_small]
KEY = secrets.token_hex(16)  # generated per run; no key-shaped literal in the repo
ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}


@pytest.mark.parametrize("model", MODELS)
async def test_proxy_tool_call_round_trip(model: str) -> None:
    repo = InMemoryMeterRepository()
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as upstream:
        providers: dict[str, list[Provider]] = {
            m: [OllamaProvider(SETTINGS.ollama_base_url, m, upstream)] for m in MODELS
        }
        app = create_meter_app(
            providers=providers,
            keys=parse_keys(f"live-agent:{KEY}"),
            repo=repo,
            breaker=CircuitBreaker(),
            config=MeterConfig(max_tokens_per_call=256, deadline_s=180.0),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://meter.test", timeout=200
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {KEY}", "X-Bourse-Task-Id": "live-1"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Use the add tool. Do not answer in text."},
                        {"role": "user", "content": "What is 17 + 25?"},
                    ],
                    "tools": [ADD_TOOL],
                    "temperature": 0,
                },
            )
    assert resp.status_code == 200, resp.text
    message = resp.json()["choices"][0]["message"]
    print(model, json.dumps(message), json.dumps(resp.json()["usage"]))
    assert message.get("tool_calls"), message
    call = message["tool_calls"][0]["function"]
    assert call["name"] == "add"
    assert isinstance(json.loads(call["arguments"]), dict)
    rows = await repo.list_usage()
    assert len(rows) == 1
    assert rows[0].tokens_in > 0 and rows[0].tokens_out > 0
    assert (rows[0].agent_id, rows[0].task_id, rows[0].status) == ("live-agent", "live-1", 200)
