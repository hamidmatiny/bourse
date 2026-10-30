"""Terminal approver. Only "y"/"yes" approves; anything else, or a timeout, denies."""

import builtins
from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from anyio import to_thread

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest


class CliApprover:
    def __init__(self, input_fn: Callable[[str], str] = builtins.input, name: str = "cli") -> None:
        self._input_fn = input_fn
        self._name = name

    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        prompt = (
            f"[approval {req.trigger.value}] {req.summary} (subject={req.subject_id}) "
            f"approve? [y/N] (auto-deny in {timeout_s:.0f}s): "
        )
        answer: str | None = None
        with anyio.move_on_after(timeout_s):
            answer = await to_thread.run_sync(self._input_fn, prompt, abandon_on_cancel=True)
        now = datetime.now(UTC)
        if answer is None:
            return ApprovalDecision(approved=False, by=self._name, at=now, reason="timeout")
        approved = answer.strip().lower() in {"y", "yes"}
        reason = "operator approved" if approved else f"operator answered {answer.strip()!r}"
        return ApprovalDecision(approved=approved, by=self._name, at=now, reason=reason)
