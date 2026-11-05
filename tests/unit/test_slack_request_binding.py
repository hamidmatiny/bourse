"""A click only counts for the pending request whose request_id it carries (ADR-0048)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from bourse.approval.facts import make_request
from bourse.approval.message import APPROVE_ACTION, DENY_ACTION, build_blocks
from bourse.approval.slack import FakeSlackClient, SlackApprover
from bourse.contracts.approval import ApprovalFacts, ApprovalRequest, ApprovalTrigger

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
AGENT = "sleeper-loud@vendor-x.local"


def _req() -> ApprovalRequest:
    return make_request(
        ApprovalTrigger.REINSTATE_AGENT,
        ApprovalFacts(agent_id=AGENT, exam_n=5, exam_total=5),
        created_at=NOW,
    )


def _approver(fake: FakeSlackClient, lines: list[str]) -> SlackApprover:
    return SlackApprover(
        channel_id="C1",
        approver_ids=frozenset({"U_OK"}),
        client=fake,
        clock=lambda: NOW,
        echo=lines.append,
    )


async def test_click_on_older_request_is_expired_and_does_not_resolve_current() -> None:
    fake = FakeSlackClient()
    lines: list[str] = []
    approver = _approver(fake, lines)
    old = _req()
    # Old request times out (its message keeps its own request_id on the buttons).
    d_old = await approver.request(old, timeout_s=0.01)
    assert (d_old.reason, d_old.request_id, d_old.action) == ("timeout", old.request_id, "timeout")
    cur = _req()
    task = asyncio.create_task(approver.request(cur, timeout_s=2.0))
    await asyncio.sleep(0.02)
    # Deny on the OLD message: ignored, answered "expired", current stays pending.
    await approver.handle_action(request_id=old.request_id, user_id="U_OK", approved=False)
    assert fake.ephemerals[-1]["text"].startswith(f"expired: request_id {old.request_id}")
    assert not task.done()
    assert any("reason=expired_or_unknown" in ln and "action=deny" in ln for ln in lines)
    # APPROVE on the current message resolves it, with request_id + action recorded.
    await approver.handle_action(request_id=cur.request_id, user_id="U_OK", approved=True)
    decision = await task
    assert decision.approved and decision.by == "U_OK"
    assert (decision.request_id, decision.action) == (cur.request_id, "approve")
    assert any(f"slack decision action=approve request_id={cur.request_id}" in ln for ln in lines)


async def test_settled_request_click_is_expired() -> None:
    fake = FakeSlackClient()
    approver = _approver(fake, [])
    req = _req()
    task = asyncio.create_task(approver.request(req, timeout_s=2.0))
    await asyncio.sleep(0.02)
    await approver.handle_action(request_id=req.request_id, user_id="U_OK", approved=True)
    assert (await task).approved
    await approver.handle_action(request_id=req.request_id, user_id="U_OK", approved=False)
    assert fake.ephemerals[-1]["text"].startswith("expired:")


def test_message_shows_action_agent_request_id_and_approve_button() -> None:
    req = _req()
    blocks = build_blocks(req, with_buttons=True)
    head = blocks[1]["text"]["text"]
    assert "*Action:* reinstate agent (QUARANTINED -> ACTIVE)" in head
    assert f"*Agent:* `{AGENT}`" in head
    assert f"*request_id:* `{req.request_id}`" in head
    buttons = blocks[-1]["elements"]
    approve = next(b for b in buttons if b["action_id"] == APPROVE_ACTION)
    deny = next(b for b in buttons if b["action_id"] == DENY_ACTION)
    assert approve["text"]["text"] == f"APPROVE {req.request_id[:8]}"
    assert approve["style"] == "primary"
    assert approve["value"] == deny["value"] == req.request_id
    assert "confirm" in deny and "confirm" not in approve


def test_host_uses_compose_approver_not_a_second_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import StringIO

    from bourse.approval.http_client import HttpApprover
    from bourse.approval.select import select_host_approver
    from bourse.config import Settings
    from bourse.stack import launcher

    monkeypatch.setattr(launcher, "RUN_DIR", tmp_path)
    monkeypatch.setattr(launcher, "MODE_FILE", tmp_path / "mode")
    launcher.set_mode("compose")
    settings = Settings(
        slack_bot_token=SecretStr("xoxb-test"),
        slack_app_token=SecretStr("xapp-test"),
        slack_channel_id="C1",
        bourse_slack_approvers="U_OK",
        bourse_approver_key=SecretStr("k"),
    )
    out = StringIO()
    approver = select_host_approver(settings, echo=out)
    assert isinstance(approver, HttpApprover)
    assert approver.base_url == "http://127.0.0.1:55310"
    assert "only Slack consumer" in out.getvalue()


async def test_new_reinstate_exam_clears_stale_result() -> None:
    from bourse.contracts.exam import ExamReport
    from bourse.trust.exam import ExamScheduler

    class Runner:
        async def run_for_reinstate(self, agent_id: str) -> None:
            await asyncio.sleep(3600)

        def hard_cap_s(self, agent_id: str) -> float:
            return 3600.0

    # Duck-typed ExamRunner: only the two methods schedule_reinstate touches.
    sched = ExamScheduler(Runner(), echo=lambda _m: None)  # type: ignore[arg-type]
    sched.reinstate_results[AGENT] = ExamReport(
        agent_id=AGENT, model="m", passed=True, n=5, total=5
    )
    assert sched.schedule_reinstate(AGENT)
    assert AGENT not in sched.reinstate_results
    for t in sched.running.values():
        t.cancel()
