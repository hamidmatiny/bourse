"""Slack Block Kit payloads from ApprovalFacts only (ADR-0048)."""

from typing import Any

from bourse.approval.facts import NOTIFY_TRIGGERS, format_summary
from bourse.contracts.approval import ApprovalFacts, ApprovalRequest, ApprovalTrigger

APPROVE_ACTION = "bourse_approve"
DENY_ACTION = "bourse_deny"


def _kv_lines(trigger: ApprovalTrigger, facts: ApprovalFacts) -> list[str]:
    lines = [f"*trigger:* `{trigger.value}`"]
    if facts.agent_id:
        lines.append(f"*agent_id:* `{facts.agent_id}`")
    if facts.vendor:
        lines.append(f"*vendor:* `{facts.vendor}`")
    if facts.violation_kinds:
        parts = ", ".join(f"`{v.kind}` x{v.count}" for v in facts.violation_kinds)
        lines.append(f"*violations:* {parts}")
    if facts.strikes is not None:
        lines.append(f"*strikes:* `{facts.strikes}`")
    if facts.exposure is not None:
        lines.append(f"*exposure:* `{facts.exposure}`")
    if facts.exam_n is not None and facts.exam_total is not None:
        lines.append(f"*exam:* `{facts.exam_n}/{facts.exam_total}`")
    if facts.wilson_lb is not None:
        lines.append(f"*wilson_lb:* `{facts.wilson_lb:.4f}`")
    if facts.projected_usd is not None:
        lines.append(f"*projected_usd:* `{facts.projected_usd}`")
    if facts.usd_spent is not None:
        lines.append(f"*usd_spent:* `{facts.usd_spent}`")
    if facts.usd_cap is not None:
        lines.append(f"*usd_cap:* `{facts.usd_cap}`")
    if facts.abort_prefix:
        lines.append(f"*abort:* `{facts.abort_prefix}`")
    return lines


ACTION_TEXT = {
    ApprovalTrigger.REINSTATE_AGENT: "reinstate agent (QUARANTINED -> ACTIVE)",
    ApprovalTrigger.PAID_CALL: "allow paid LLM call",
    ApprovalTrigger.HIGH_REP_VIOLATION: "notice: HARD violation on a high-reputation agent",
    ApprovalTrigger.ABORT_NOTICE: "notice: run aborted",
}


def build_blocks(req: ApprovalRequest, *, with_buttons: bool) -> list[dict[str, Any]]:
    """Action, agent and request_id first; then facts; then APPROVE / Deny buttons.

    Every button carries this request's request_id; a click only counts for that request.
    """
    head = [
        f"*Action:* {ACTION_TEXT.get(req.trigger, req.trigger.value)}",
        f"*Agent:* `{req.facts.agent_id or req.subject_id or '-'}`",
        f"*request_id:* `{req.request_id}`",
    ]
    text = "\n".join(_kv_lines(req.trigger, req.facts))
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"BOURSE {req.trigger.value}", "emoji": False},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(head)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"request_id=`{req.request_id}` subject=`{req.subject_id}`",
                }
            ],
        },
    ]
    if with_buttons:
        short = req.request_id[:8]
        blocks.append(
            {
                "type": "actions",
                "block_id": f"bourse_{req.request_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": APPROVE_ACTION,
                        "text": {
                            "type": "plain_text",
                            "text": f"APPROVE {short}",
                            "emoji": False,
                        },
                        "style": "primary",
                        "value": req.request_id,
                    },
                    {
                        "type": "button",
                        "action_id": DENY_ACTION,
                        "text": {"type": "plain_text", "text": f"Deny {short}", "emoji": False},
                        "style": "danger",
                        "value": req.request_id,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Deny this request?"},
                            "text": {
                                "type": "plain_text",
                                "text": f"Deny {req.trigger.value} request_id {req.request_id}",
                            },
                            "confirm": {"type": "plain_text", "text": "Deny"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                ],
            }
        )
    return blocks


def build_fallback_text(req: ApprovalRequest) -> str:
    """Always from facts; ignore req.summary so free text cannot leak."""
    return format_summary(req.trigger, req.facts)


def wants_buttons(trigger: ApprovalTrigger) -> bool:
    return trigger not in NOTIFY_TRIGGERS
