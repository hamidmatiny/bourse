"""Ollama capability probe report (ADR-0006)."""

from pydantic import BaseModel, ConfigDict, Field


class ProbeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ollama_version: str | None
    models: tuple[str, ...]
    format_json_status: int | None
    tool_call_ok: bool
    think_false_ok: bool
    resident_bytes: int = Field(ge=0)
    resident_ok: bool
    latency_ms: float | None
