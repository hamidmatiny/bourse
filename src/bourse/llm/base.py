"""Provider protocol and LLM-layer value types."""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bourse.contracts.metering import Usage

FinishReason = Literal["stop", "length", "tool_calls"]


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: dict[str, Any] | str  # str when the model emitted unparseable JSON
    id: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()  # assistant turns that called tools
    tool_call_id: str | None = None  # tool turns (OpenAI wire format)
    tool_name: str | None = None  # tool turns (Ollama wire format)


class ChatResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage
    usd: Decimal = Field(default=Decimal(0), ge=0)
    finish_reason: FinishReason = "stop"


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def paid(self) -> bool: ...

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult: ...


def tool_payload(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Tool list in the shape shared by Ollama /api/chat and OpenAI chat completions."""
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def parse_arguments(raw: Any) -> dict[str, Any] | str:
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items()}
        return raw
    return str(raw)


def _arguments_dict(arguments: dict[str, Any] | str) -> dict[str, Any]:
    parsed = parse_arguments(arguments)
    return parsed if isinstance(parsed, dict) else {}


def ollama_message(m: ChatMessage) -> dict[str, Any]:
    """ChatMessage in Ollama /api/chat form. Plain turns are exactly {role, content}."""
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {"function": {"name": c.name, "arguments": _arguments_dict(c.arguments)}}
            for c in m.tool_calls
        ]
    if m.role == "tool" and m.tool_name:
        out["tool_name"] = m.tool_name
    return out


def openai_message(m: ChatMessage) -> dict[str, Any]:
    """ChatMessage in OpenAI chat-completions form. Plain turns are exactly {role, content}."""
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id or f"call_{i}",
                "type": "function",
                "function": {
                    "name": c.name,
                    "arguments": c.arguments
                    if isinstance(c.arguments, str)
                    else json.dumps(c.arguments),
                },
            }
            for i, c in enumerate(m.tool_calls)
        ]
    if m.role == "tool" and m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out
