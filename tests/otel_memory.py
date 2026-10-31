"""In-memory span capture for tests. The global tracer provider can be set once per process."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))


def memory_exporter() -> InMemorySpanExporter:
    if trace.get_tracer_provider() is not _PROVIDER:
        trace.set_tracer_provider(_PROVIDER)
    assert trace.get_tracer_provider() is _PROVIDER, "another tracer provider was set first"
    _EXPORTER.clear()
    return _EXPORTER
