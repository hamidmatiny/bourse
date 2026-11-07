"""Deterministic prompt-token estimate for the meter's hard per-task cap (ADR-0041 amendment).

The meter admits a call only if `estimate + max_tokens <= remaining`. The real prompt count is
known only after the call (Ollama `prompt_eval_count`), so the estimate is built to be at or
above it for the roster model (Qwen BPE):
- every ASCII digit counts 1 token (the Qwen pre-tokenizer splits digits one by one);
- every other byte (UTF-8) counts 1/2 token, rounded up (English text is about 4 bytes per
  token, JSON and CSV punctuation about 1.5-2);
- each message adds MESSAGE_OVERHEAD (role markers, template), each tool schema is counted as
  its JSON plus TOOL_OVERHEAD, and a request with tools adds TOOLS_PREAMBLE (the template's
  tool-use instructions).

The estimate is not a tokenizer. The meter records it next to the measured `tokens_in` on
every usage row, so how often it falls below the real count is measured, not assumed.
"""

import json
import math
from collections.abc import Sequence

from bourse.llm.base import ChatMessage, ToolSpec

MESSAGE_OVERHEAD = 16
TOOL_OVERHEAD = 16
TOOLS_PREAMBLE = 256
REQUEST_OVERHEAD = 32


def _text_tokens(text: str) -> int:
    if not text:
        return 0
    data = text.encode("utf-8")
    digits = sum(48 <= b <= 57 for b in data)
    return digits + math.ceil((len(data) - digits) / 2)


def _message_text(m: ChatMessage) -> str:
    parts = [m.role, m.content]
    for call in m.tool_calls:
        args = call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
        parts += [call.name, args]
    if m.tool_name:
        parts.append(m.tool_name)
    return "\n".join(parts)


def estimate_prompt_tokens(messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> int:
    total = REQUEST_OVERHEAD
    for m in messages:
        total += MESSAGE_OVERHEAD + _text_tokens(_message_text(m))
    for t in tools:
        total += TOOL_OVERHEAD + _text_tokens(json.dumps(t.model_dump(mode="json")))
    if tools:
        total += TOOLS_PREAMBLE
    return total
