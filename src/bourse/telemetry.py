"""OpenTelemetry tracing (ADR-0032): W3C traceparent across broker, agents, meter, MCP servers.

- setup_tracing(): SDK TracerProvider + OTLP gRPC exporter to the compose collector. Skipped
  when OTEL_SDK_DISABLED=true (the CrewAI agent process, ADR-0021); trace context is still
  propagated there because extracted parents are passed through unchanged.
- Outgoing HTTP: httpx event hook `inject_traceparent` / `trace_headers()` for clients that
  take explicit headers (the MCP adapter).
- Incoming HTTP: `TraceMiddleware` extracts the parent and opens one SERVER span per request.
"""

import os
from typing import Any

import httpx
from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACER_NAME = "bourse"


def setup_tracing(service_name: str, endpoint: str) -> TracerProvider | None:
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> Tracer:
    return trace.get_tracer(TRACER_NAME)


def trace_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


async def inject_traceparent(request: httpx.Request) -> None:
    request.headers.update(trace_headers())


def traced_async_client(**kwargs: Any) -> httpx.AsyncClient:
    hooks = kwargs.pop("event_hooks", {}) or {}
    requests = [*hooks.get("request", []), inject_traceparent]
    return httpx.AsyncClient(event_hooks={**hooks, "request": requests}, **kwargs)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


class TraceMiddleware:
    """One SERVER span per HTTP request whose method is in `methods`; parent from traceparent."""

    def __init__(self, app: ASGIApp, span_name: str, methods: frozenset[str] = frozenset({"POST"})):
        self.app = app
        self.span_name = span_name
        self.methods = methods

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") not in self.methods:
            await self.app(scope, receive, send)
            return
        carrier = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers") or []}
        parent = propagate.extract(carrier)
        status_code = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 0))
            await send(message)

        token = context.attach(parent)
        try:
            span = tracer().start_span(self.span_name, kind=SpanKind.SERVER)
            if not span.get_span_context().is_valid:
                # SDK disabled in this process (CrewAI agent): keep the incoming parent current
                # so outgoing calls still carry the run's traceparent.
                await self.app(scope, receive, send_wrapper)
                return
            with trace.use_span(span, end_on_exit=True):
                span.set_attribute("http.route", str(scope.get("path", "")))
                await self.app(scope, receive, send_wrapper)
                span.set_attribute("http.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
        finally:
            context.detach(token)
