"""Structured output via a single `submit` tool (ADR-0006).

Ollama 0.40.6 returns 501 on every `format` path, so the model must call `submit` with JSON
arguments validated by a Pydantic model. Validation errors are fed back; after max_attempts
the caller gets a typed SubmitFailed.
"""

import json
from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from bourse.llm.base import ChatMessage, ChatResult, ToolSpec

T = TypeVar("T", bound=BaseModel)

SUBMIT_TOOL_NAME = "submit"


class ChatClient(Protocol):
    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult: ...


class SubmitFailed(RuntimeError):
    def __init__(self, attempts: int, last_error: str) -> None:
        super().__init__(f"submit failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


def submit_tool_spec(schema: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_TOOL_NAME,
        description=f"Submit the final answer as a {schema.__name__} object. Call exactly once.",
        parameters=schema.model_json_schema(),
    )


async def submit(
    router: ChatClient,
    messages: Sequence[ChatMessage],
    schema: type[T],
    max_attempts: int = 3,
    *,
    max_tokens: int = 1024,
    deadline_s: float = 120.0,
) -> T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    tool = submit_tool_spec(schema)
    convo = list(messages)
    last_error = ""
    for _attempt in range(max_attempts):
        result = await router.chat(convo, [tool], max_tokens, deadline_s)
        calls = [c for c in result.tool_calls if c.name == SUBMIT_TOOL_NAME]
        if not calls:
            last_error = "no submit tool call"
            if result.text:
                convo.append(ChatMessage(role="assistant", content=result.text))
            convo.append(
                ChatMessage(
                    role="user",
                    content=f"You did not call the `{SUBMIT_TOOL_NAME}` tool. "
                    f"Call `{SUBMIT_TOOL_NAME}` exactly once with arguments matching its schema.",
                )
            )
            continue
        args = calls[0].arguments
        try:
            if isinstance(args, str):
                return schema.model_validate_json(args)
            return schema.model_validate(args)
        except ValidationError as exc:
            last_error = str(exc)
            shown = args if isinstance(args, str) else json.dumps(args, default=str)
            convo.append(
                ChatMessage(
                    role="user",
                    content=f"Your `{SUBMIT_TOOL_NAME}` arguments were invalid.\n"
                    f"Arguments: {shown}\nValidation error: {last_error}\n"
                    f"Call `{SUBMIT_TOOL_NAME}` again with corrected arguments.",
                )
            )
    raise SubmitFailed(max_attempts, last_error)
