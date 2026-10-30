import threading
import time
from datetime import UTC, datetime

from bourse.approval.base import Approver
from bourse.approval.cli import CliApprover
from bourse.approval.deny import DenyApprover
from bourse.contracts.approval import ApprovalRequest, ApprovalTrigger

REQ = ApprovalRequest(
    request_id="q1",
    trigger=ApprovalTrigger.REINSTATE_AGENT,
    subject_id="agent-7",
    summary="reinstate agent-7",
    created_at=datetime(2026, 10, 29, tzinfo=UTC),
)


async def test_deny_approver_denies() -> None:
    approver: Approver = DenyApprover()
    decision = await approver.request(REQ, timeout_s=1.0)
    assert decision.approved is False
    assert decision.reason == "unattended"


async def test_cli_approver_approves_on_y() -> None:
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    approver: Approver = CliApprover(input_fn=answer)
    decision = await approver.request(REQ, timeout_s=1.0)
    assert decision.approved is True
    assert decision.by == "cli"
    assert "REINSTATE_AGENT" in prompts[0]


async def test_cli_approver_denies_on_n() -> None:
    decision = await CliApprover(input_fn=lambda _p: "n").request(REQ, timeout_s=1.0)
    assert decision.approved is False


async def test_cli_approver_denies_on_timeout() -> None:
    release = threading.Event()

    def never_answers(prompt: str) -> str:
        release.wait(5.0)
        return "y"

    started = time.monotonic()
    try:
        decision = await CliApprover(input_fn=never_answers).request(REQ, timeout_s=0.2)
    finally:
        release.set()
    assert decision.approved is False
    assert decision.reason == "timeout"
    assert time.monotonic() - started < 1.0
