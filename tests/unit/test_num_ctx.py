"""options.num_ctx has one source: BOURSE_NUM_CTX_BY_MODEL (ADR-0038)."""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from bourse.config import Settings, get_settings
from bourse.llm.base import ChatMessage
from bourse.llm.ollama import OllamaProvider
from bourse.llm.probe import run_probe
from bourse.meter.app import MeterConfig, create_meter_app
from bourse.meter.repo import InMemoryMeterRepository

SRC = Path(__file__).resolve().parents[2] / "src" / "bourse"
# The map lives in config.py; meter/stats.py holds the 4096/6144/8192 decision rule.
ALLOWED = {SRC / "config.py", SRC / "meter" / "stats.py"}
MODELS = ["gemma4:12b", "qwen3.5:27b-q4_K_M"]
OLLAMA = "http://ollama.test"


def test_no_num_ctx_literal_outside_config() -> None:
    assign = re.compile(r"num_ctx[\"']?\s*[:=]\s*\d")
    sizes = re.compile(r"\b(4096|6144|8192)\b")
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in ALLOWED:
            continue
        text = path.read_text()
        if assign.search(text) or sizes.search(text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_map_parsing_and_fallback() -> None:
    s = Settings(bourse_num_ctx_by_model="a:1=4096,b=6144,*=8192")
    assert (s.num_ctx_for("a:1"), s.num_ctx_for("b"), s.num_ctx_for("other")) == (4096, 6144, 8192)
    with pytest.raises(KeyError):
        Settings(bourse_num_ctx_by_model="a=4096").num_ctx_for("b")


def _capture(bodies: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "tool_calls": [
                            {"function": {"name": "submit", "arguments": {"answer": 5}}}
                        ],
                    },
                    "done_reason": "stop",
                    "prompt_eval_count": 3,
                    "eval_count": 2,
                },
            )
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.6"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in MODELS]})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("model", MODELS)
async def test_every_ollama_path_sends_the_mapped_value(model: str) -> None:
    expected = get_settings().num_ctx_for(model)
    bodies: list[dict[str, Any]] = []
    async with httpx.AsyncClient(transport=_capture(bodies)) as client:
        # 1. direct provider (live tests, router chains)
        await OllamaProvider(OLLAMA, model, client).chat(
            [ChatMessage(role="user", content="hi")], [], 8, 10.0
        )
        # 2. metering proxy (agents and the broker planner reach Ollama only through it)
        key = "k" * 32
        app = create_meter_app(
            providers={model: [OllamaProvider(OLLAMA, model, client)]},
            keys={key: "agent"},
            repo=InMemoryMeterRepository(),
            config=MeterConfig(max_tokens_per_call=16, deadline_s=5.0),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://meter.test"
        ) as meter:
            resp = await meter.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status_code == 200
        # 3. capability probe (two chat calls)
        await run_probe(client, OLLAMA, model)
    assert len(bodies) == 4
    assert [b["options"]["num_ctx"] for b in bodies] == [expected] * 4
