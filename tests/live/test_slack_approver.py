"""Live Slack approval: sends a real message and waits for an operator click."""

from __future__ import annotations

import os

import pytest

from bourse.approval.facts import make_request
from bourse.approval.select import select_approver
from bourse.approval.slack import SlackApprover
from bourse.config import get_settings
from bourse.contracts.approval import ApprovalFacts, ApprovalTrigger

pytestmark = pytest.mark.slack


@pytest.mark.asyncio
async def test_live_slack_approval_click() -> None:
    """Requires Slack env + a human click on Approve or Deny within timeout."""
    settings = get_settings()
    if not settings.slack_env_complete:
        pytest.skip("Slack env incomplete (tokens/channel/approvers)")
    timeout = float(os.environ.get("BOURSE_SLACK_LIVE_TIMEOUT_S", "120"))
    approver = select_approver(settings, start_socket=True)
    assert isinstance(approver, SlackApprover)
    req = make_request(
        ApprovalTrigger.REINSTATE_AGENT,
        ApprovalFacts(
            agent_id="live-slack-test",
            vendor="bourse.test",
            exam_n=0,
            exam_total=0,
            strikes=0,
            exposure=0,
        ),
    )
    await approver.start()
    try:
        decision = await approver.request(req, timeout)
    finally:
        await approver.stop()
    # Either Approve or Deny is a valid human response; timeout fails the test.
    assert decision.reason != "timeout"
    assert decision.reason != "slack_unreachable"
    assert decision.by.startswith("U") or decision.by  # Slack user id
