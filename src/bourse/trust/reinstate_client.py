"""Host-driven reinstatement over the registry API (ADR-0048).

1. POST /agents/{id}/reinstate-exam (registry runs the exam in-network).
2. Poll GET /agents/{id}/reinstate-exam-result until a result or the poll deadline.
3. Exam failed -> stop; no approval is requested.
4. Exam passed -> REINSTATE_AGENT approval via the given Approver (operator banner first).
5. POST /agents/{id}/reinstate with the ApprovalDecision; registry applies ACTIVE + rel_epoch
   bump on approve, or an audited stay-QUARANTINED row on deny / timeout.

Used by `bourse trust reinstate` and by the hermetic 5c state-machine test.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from bourse.approval.base import Approver
from bourse.approval.facts import make_request
from bourse.contracts.approval import ApprovalDecision, ApprovalFacts, ApprovalTrigger
from bourse.contracts.exam import ExamReport
from bourse.trust.reliability_epoch import reliability_epoch


@dataclass(frozen=True)
class HostReinstateResult:
    """status: active | denied | exam_failed | exam_timeout | blocked"""

    status: str
    exam: ExamReport | None = None
    decision: ApprovalDecision | None = None
    request_id: str | None = None
    epoch_before: int | None = None
    epoch_after: int | None = None

    @property
    def rc(self) -> int:
        return 0 if self.status == "active" else 1


CLICK_EXPECT = ("approve", "deny", "none")


def click_banner(request_id: str, trigger: str, agent_id: str, expect: str = "approve") -> str:
    """Operator banner printed right before the Slack request is posted."""
    short = request_id[:8]
    what = {
        "approve": f"CLICK APPROVE in Slack  ->  button 'APPROVE {short}'",
        "deny": f"CLICK DENY in Slack  ->  button 'Deny {short}', then confirm",
        "none": "DO NOT CLICK  (timeout test; message must turn EXPIRED)",
    }[expect]
    bar = "#" * 72
    return "\n".join(
        [
            bar,
            f"#  {what}",
            f"#  action={trigger} agent={agent_id}",
            f"#  request_id={request_id}",
            "#  (a click on any other message is ignored and answered 'expired')",
            bar,
        ]
    )


async def host_reinstate(
    http: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    agent_id: str,
    approver: Approver,
    *,
    reason: str,
    timeout_s: float,
    echo: Callable[[str], None] = print,
    poll_s: float = 2.0,
    poll_timeout_s: float = 1200.0,
    expect: str = "approve",
) -> HostReinstateResult:
    base = base.rstrip("/")
    start = await http.post(f"{base}/agents/{agent_id}/reinstate-exam", headers=headers)
    if start.status_code == 409:
        echo(f"reinstate status=blocked detail={start.text}")
        return HostReinstateResult("blocked")
    start.raise_for_status()
    echo(f"reinstate-exam started agent_id={agent_id}")
    report: ExamReport | None = None
    deadline = time.monotonic() + poll_timeout_s
    while time.monotonic() < deadline:
        resp = await http.get(f"{base}/agents/{agent_id}/reinstate-exam-result", headers=headers)
        if resp.status_code == 200:
            report = ExamReport.model_validate(resp.json())
            break
        if resp.status_code != 404:
            resp.raise_for_status()
        await asyncio.sleep(poll_s)
    if report is None:
        echo(f"reinstate status=exam_timeout detail=no result within {poll_timeout_s:.0f}s")
        return HostReinstateResult("exam_timeout")
    echo(f"reinstate-exam passed={report.passed} exam={report.n}/{report.total}")
    if not report.passed:
        echo(
            f"reinstate status=exam_failed exam={report.n}/{report.total} "
            "detail=stays QUARANTINED; no approval requested "
            "(without /bourse/reset-behavior a turned sleeper stays 0/5)"
        )
        return HostReinstateResult("exam_failed", exam=report)

    echo(f"exam passed {report.n}/{report.total} -> requesting approval")
    req = make_request(
        ApprovalTrigger.REINSTATE_AGENT,
        ApprovalFacts(agent_id=agent_id, exam_n=report.n, exam_total=report.total),
    )
    echo(click_banner(req.request_id, req.trigger.value, agent_id, expect))
    decision = await approver.request(req, timeout_s)

    pre = await http.get(f"{base}/agents/{agent_id}", headers=headers)
    pre.raise_for_status()
    before_epoch = reliability_epoch(tuple(pre.json().get("tags") or ()))
    apply = await http.post(
        f"{base}/agents/{agent_id}/reinstate",
        headers=headers,
        json={"reason": reason.strip(), "approval": decision.model_dump(mode="json")},
    )
    if apply.status_code not in (200, 409):
        apply.raise_for_status()
    body = apply.json()
    to_state = body.get("to_state", "?")
    status = "active" if to_state == "ACTIVE" else "denied"
    echo(
        f"reinstate status={status} exam={report.n}/{report.total} "
        f"approved={decision.approved} by={decision.by} reason={decision.reason} "
        f"request_id={decision.request_id or req.request_id} "
        f"action={decision.action or '-'} to_state={to_state}"
    )
    if status == "denied":
        echo(
            f"audit: stays QUARANTINED; approver_id={decision.by} "
            f"actor={body.get('actor', 'reinstate')}"
        )
        return HostReinstateResult(
            "denied", exam=report, decision=decision, request_id=req.request_id
        )
    live = await http.get(f"{base}/agents/{agent_id}", headers=headers)
    live.raise_for_status()
    tags = tuple(live.json().get("tags") or ())
    after_epoch = reliability_epoch(tags)
    echo(f"rel_epoch {before_epoch} -> {after_epoch} tags={list(tags)}")
    return HostReinstateResult(
        "active",
        exam=report,
        decision=decision,
        request_id=req.request_id,
        epoch_before=before_epoch,
        epoch_after=after_epoch,
    )
