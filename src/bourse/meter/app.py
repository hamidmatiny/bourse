"""Metering LLM proxy (ADR-0017): OpenAI-compatible facade over the Phase 0 router.

The proxy, not the agent, enforces: per-agent bearer keys, model allowlist, think=false (Ollama
provider), max_tokens <= BudgetSpec.max_tokens_per_call, no response_format, the global
circuit breaker, and a wall-clock deadline. Usage rows carry upstream-measured token counts.
"""

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace

from bourse.contracts.metering import BudgetSpec, LlmUsageRecord
from bourse.llm.base import ChatResult, Provider, Sampling
from bourse.llm.ollama import OllamaProvider
from bourse.llm.omniroute import OpenAICompatProvider
from bourse.llm.router import AllProvidersFailed, Router
from bourse.meter.keys import agent_for
from bourse.meter.openai_wire import (
    WireError,
    completion_body,
    error_body,
    parse_request,
    stream_body,
)
from bourse.meter.repo import MeterRepository
from bourse.runtime.breaker import GLOBAL_BREAKER, BreakerOpen, CircuitBreaker, ProviderHTTPError
from bourse.runtime.budget import BudgetExceeded, BudgetLedger
from bourse.runtime.deadline import DeadlineExceeded
from bourse.telemetry import TraceMiddleware

TASK_HEADER = "x-bourse-task-id"
SEED_HEADER = "x-bourse-seed"
METER_PORT = 55200


@dataclass(frozen=True)
class MeterConfig:
    max_tokens_per_call: int = 2048
    deadline_s: float = 120.0
    # Per-request ledger; run-level per-agent budgets arrive with the ledger in Phase 4.
    max_tokens_run: int = 10**12
    # Sampling is set here, never by clients (ADR-0020). A request may pick the seed with
    # X-Bourse-Seed; temperature/top_p/seed fields in the request body are ignored.
    sampling: Sampling = field(default_factory=Sampling)


def with_sampling(provider: Provider, sampling: Sampling) -> Provider:
    if isinstance(provider, OllamaProvider | OpenAICompatProvider):
        return provider.with_sampling(sampling)
    return provider


def _now() -> datetime:
    return datetime.now(UTC)


def create_meter_app(
    *,
    providers: Mapping[str, Sequence[Provider]],
    keys: Mapping[str, str],
    repo: MeterRepository,
    breaker: CircuitBreaker = GLOBAL_BREAKER,
    config: MeterConfig | None = None,
    clock: Callable[[], datetime] = _now,
) -> FastAPI:
    """`providers` maps each allowlisted model to its ordered provider chain."""
    cfg = config or MeterConfig()
    app = FastAPI(title="BOURSE metering proxy", version="0.1.0")
    app.add_middleware(TraceMiddleware, span_name="llm call")

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        found = agent_for(request.headers.get("authorization"), keys)
        if found is None:
            return JSONResponse(error_body("unknown or missing agent key", "unauthorized"), 401)
        agent_id: str = found
        task_id = request.headers.get(TASK_HEADER)
        started = time.monotonic()
        model = ""

        async def record(
            status: int, error_kind: str | None, result: ChatResult | None = None
        ) -> None:
            span = trace.get_current_span()
            span.set_attribute("agent_id", agent_id)
            span.set_attribute("task_id", task_id or "")
            span.set_attribute("model", result.usage.model if result else model)
            span.set_attribute("tokens_in", result.usage.tokens_in if result else 0)
            span.set_attribute("tokens_out", result.usage.tokens_out if result else 0)
            span.set_attribute("outcome", error_kind or "ok")
            await repo.add_usage(
                LlmUsageRecord(
                    agent_id=agent_id,
                    task_id=task_id,
                    provider=result.usage.provider if result else None,
                    model=result.usage.model if result else model,
                    tokens_in=result.usage.tokens_in if result else 0,
                    tokens_out=result.usage.tokens_out if result else 0,
                    wall_ms=(time.monotonic() - started) * 1000,
                    status=status,
                    error_kind=error_kind,
                    at=clock(),
                )
            )

        async def fail(status: int, kind: str, message: str) -> JSONResponse:
            await record(status, kind)
            return JSONResponse(error_body(message, kind), status)

        try:
            body = await request.json()
        except ValueError:
            return await fail(400, "bad_request", "body is not valid JSON")
        if isinstance(body, dict) and isinstance(body.get("model"), str):
            model = body["model"]
        try:
            parsed = parse_request(body)
        except WireError as exc:
            return await fail(400, exc.kind, str(exc))
        if parsed.model not in providers:
            return await fail(400, "model_not_allowed", f"model {parsed.model!r} is not allowed")

        sampling = cfg.sampling
        raw_seed = request.headers.get(SEED_HEADER)
        if raw_seed is not None:
            if not raw_seed.lstrip("-").isdigit():
                return await fail(400, "bad_request", "X-Bourse-Seed must be an integer")
            sampling = sampling.model_copy(update={"seed": int(raw_seed)})
        chain = [with_sampling(p, sampling) for p in providers[parsed.model]]
        spec = BudgetSpec(
            max_tokens_per_call=cfg.max_tokens_per_call,
            max_tokens_run=cfg.max_tokens_run,
            wall_budget_s=cfg.deadline_s,
        )
        router = Router(chain, BudgetLedger(spec, run_id=agent_id), breaker)
        try:
            result = await router.chat(
                parsed.messages,
                parsed.tools,
                parsed.max_tokens or cfg.max_tokens_per_call,
                cfg.deadline_s,
            )
        except BreakerOpen as exc:
            return await fail(503, "breaker_open", str(exc))
        except DeadlineExceeded as exc:
            return await fail(504, "deadline", str(exc))
        except AllProvidersFailed as exc:
            return await fail(502, "upstream_failed", str(exc))
        except (ProviderHTTPError, httpx.HTTPError) as exc:
            return await fail(502, "upstream_error", str(exc))
        except BudgetExceeded as exc:
            return await fail(429, "budget", str(exc))

        await record(200, None, result)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(clock().timestamp())
        if parsed.stream:
            return Response(
                stream_body(result, parsed.model, completion_id, created),
                media_type="text/event-stream",
            )
        return JSONResponse(completion_body(result, parsed.model, completion_id, created))

    return app
