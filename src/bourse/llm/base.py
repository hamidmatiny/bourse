"""Provider protocol and LLM-layer value types."""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bourse.contracts.metering import Usage


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: dict[str, Any] | str  # str when the model emitted unparseable JSON


class ChatResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage
    usd: Decimal = Field(default=Decimal(0), ge=0)


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
