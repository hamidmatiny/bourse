from bourse.llm.base import ChatMessage, ToolCall, ToolSpec
from bourse.meter.estimate import (
    MESSAGE_OVERHEAD,
    REQUEST_OVERHEAD,
    TOOLS_PREAMBLE,
    estimate_prompt_tokens,
)


def test_digits_count_one_token_each() -> None:
    a = estimate_prompt_tokens([ChatMessage(role="user", content="1234567890")], [])
    b = estimate_prompt_tokens([ChatMessage(role="user", content="abcdefghij")], [])
    assert a - b == 5  # 10 digits -> 10; 10 letters -> 5


def test_overheads_and_tools_are_counted() -> None:
    empty = estimate_prompt_tokens([], [])
    assert empty == REQUEST_OVERHEAD
    one = estimate_prompt_tokens([ChatMessage(role="user", content="")], [])
    assert one == REQUEST_OVERHEAD + MESSAGE_OVERHEAD + 3  # "user\n" -> ceil(5/2)
    tool = ToolSpec(name="submit", description="d", parameters={"type": "object"})
    with_tool = estimate_prompt_tokens([ChatMessage(role="user", content="")], [tool])
    assert with_tool - one >= TOOLS_PREAMBLE


def test_tool_call_arguments_count() -> None:
    plain = ChatMessage(role="assistant", content="")
    called = ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(name="submit", arguments={"rows": list(range(50))}),),
    )
    assert estimate_prompt_tokens([called], []) > estimate_prompt_tokens([plain], []) + 50


def test_multibyte_text_is_not_undercounted() -> None:
    text = "é" * 100  # 200 UTF-8 bytes
    assert estimate_prompt_tokens([ChatMessage(role="user", content=text)], []) >= 100


def test_estimate_check_counts_underestimates_and_refusals() -> None:
    from datetime import UTC, datetime

    from bourse.contracts.metering import LlmUsageRecord
    from bourse.meter.stats import estimate_check

    def row(
        tin: int, est: int | None, status: int = 200, kind: str | None = None
    ) -> LlmUsageRecord:
        return LlmUsageRecord(
            agent_id="a", task_id="t", provider="p", model="m", tokens_in=tin, tokens_out=1,
            wall_ms=1.0, status=status, error_kind=kind, at=datetime.now(UTC),
            prompt_tokens_est=est,
        )  # fmt: skip

    check = estimate_check([row(50, 100), row(120, 100), row(10, None), row(0, 90, 429, "BUDGET")])
    assert (check.calls, check.under, check.budget_refusals) == (2, 1, 1)
    assert check.max_ratio == 1.2
    assert check.line().startswith("meter prompt estimate below measured tokens_in: 1/2 calls")
