"""Live tests against the host Ollama. Excluded by default; run with `uv run pytest -m live`."""

from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from bourse.config import get_settings
from bourse.contracts.metering import BudgetSpec
from bourse.llm.base import ChatMessage
from bourse.llm.ollama import DEFAULT_HTTP_TIMEOUT, OllamaProvider
from bourse.llm.probe import run_probe
from bourse.llm.router import Router
from bourse.llm.submit_tool import submit
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetLedger

pytestmark = pytest.mark.live

SETTINGS = get_settings()
MODELS = [SETTINGS.bourse_model_large, SETTINGS.bourse_model_small]


class Sum(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int


@pytest.mark.parametrize("model", MODELS)
async def test_probe_live(model: str, tmp_path: Path) -> None:
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        report, written = await run_probe(client, SETTINGS.ollama_base_url, model, tmp_path)
    print(report.model_dump_json())
    assert written is not None and written.exists()
    assert model in report.models
    assert report.tool_call_ok
    assert report.think_false_ok
    assert report.resident_ok


@pytest.mark.parametrize("model", MODELS)
async def test_submit_tool_live(model: str) -> None:
    spec = BudgetSpec(max_tokens_per_call=256, max_tokens_run=4096, wall_budget_s=300.0)
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        router = Router(
            [OllamaProvider(SETTINGS.ollama_base_url, model, client)],
            BudgetLedger(spec),
            CircuitBreaker(),
        )
        result = await submit(
            router,
            [ChatMessage(role="user", content="Add 17 and 25. Call submit with the total.")],
            Sum,
            deadline_s=180.0,
        )
    print(f"{model}: {result!r} tokens={router.ledger.tokens_used}")
    assert result.total == 42
