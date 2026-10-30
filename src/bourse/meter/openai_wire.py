"""OpenAI chat-completions wire format <-> internal ChatMessage/ToolSpec/ChatResult."""

import json
from dataclasses import dataclass
from typing import Any, Literal

from bourse.llm.base import ChatMessage, ChatResult, ToolCall, ToolSpec, parse_arguments

SUBMIT_TOOL_HINT = "use submit-tool pattern (ADR-0006)"
_ROLES: dict[str, Literal["system", "user", "assistant"]] = {
    "system": "system",
    "developer": "system",
    "user": "user",
    "assistant": "assistant",
}


class WireError(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ParsedRequest:
    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec]
    max_tokens: int | None
    stream: bool


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                raise WireError("bad_request", "only text content parts are supported")
            parts.append(str(part.get("text", "")))
        return "".join(parts)
    raise WireError("bad_request", "message content must be a string or a list of text parts")


def _tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise WireError("bad_request", "tool_calls must be a list")
    calls: list[ToolCall] = []
    for item in raw:
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise WireError("bad_request", "tool_calls[].function.name is required")
        calls.append(
            ToolCall(
                id=item.get("id"),
                name=fn["name"],
                arguments=parse_arguments(fn.get("arguments", "{}")),
            )
        )
    return tuple(calls)


def parse_request(body: Any) -> ParsedRequest:
    if not isinstance(body, dict):
        raise WireError("bad_request", "request body must be a JSON object")
    fmt = body.get("response_format")
    if fmt is not None and not (isinstance(fmt, dict) and fmt.get("type") == "text"):
        raise WireError("response_format", f"response_format is not supported; {SUBMIT_TOOL_HINT}")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise WireError("bad_request", "model is required")
    if body.get("n", 1) != 1:
        raise WireError("bad_request", "n must be 1")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise WireError("bad_request", "messages must be a non-empty list")

    messages: list[ChatMessage] = []
    names_by_id: dict[str, str] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise WireError("bad_request", "each message must be an object")
        role = raw.get("role")
        if role == "tool":
            call_id = raw.get("tool_call_id")
            if not isinstance(call_id, str):
                raise WireError("bad_request", "tool messages need tool_call_id")
            messages.append(
                ChatMessage(
                    role="tool",
                    content=_text(raw.get("content")),
                    tool_call_id=call_id,
                    tool_name=names_by_id.get(call_id) or raw.get("name"),
                )
            )
            continue
        if role not in _ROLES:
            raise WireError("bad_request", f"unsupported role {role!r}")
        calls = _tool_calls(raw.get("tool_calls")) if role == "assistant" else ()
        for c in calls:
            if c.id:
                names_by_id[c.id] = c.name
        messages.append(
            ChatMessage(role=_ROLES[role], content=_text(raw.get("content")), tool_calls=calls)
        )

    tools: list[ToolSpec] = []
    for t in body.get("tools") or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise WireError("bad_request", "tools[].function.name is required")
        tools.append(
            ToolSpec(
                name=fn["name"],
                description=str(fn.get("description") or ""),
                parameters=fn.get("parameters") or {"type": "object", "properties": {}},
            )
        )

    max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
        raise WireError("bad_request", "max_tokens must be a positive integer")
    return ParsedRequest(
        model=model,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        stream=bool(body.get("stream", False)),
    )


def _wire_tool_calls(result: ChatResult) -> list[dict[str, Any]]:
    return [
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
        for i, c in enumerate(result.tool_calls)
    ]


def _usage(result: ChatResult) -> dict[str, int]:
    u = result.usage
    return {
        "prompt_tokens": u.tokens_in,
        "completion_tokens": u.tokens_out,
        "total_tokens": u.tokens_in + u.tokens_out,
    }


def completion_body(
    result: ChatResult, model: str, completion_id: str, created: int
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
    if result.tool_calls:
        message["tool_calls"] = _wire_tool_calls(result)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
        "usage": _usage(result),
    }


def stream_body(result: ChatResult, model: str, completion_id: str, created: int) -> str:
    """stream=true: the whole completion as one SSE chunk, then [DONE] (ADR-0017)."""
    delta: dict[str, Any] = {"role": "assistant", "content": result.text or None}
    if result.tool_calls:
        delta["tool_calls"] = [
            {"index": i, **call} for i, call in enumerate(_wire_tool_calls(result))
        ]
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": result.finish_reason}],
        "usage": _usage(result),
    }
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def error_body(message: str, kind: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": kind}}
