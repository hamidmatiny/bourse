"""Approver for CI and unattended runs: always denies."""

from datetime import UTC, datetime

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest


class DenyApprover:
    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        return ApprovalDecision(
            approved=False, by="deny-approver", at=datetime.now(UTC), reason="unattended"
        )
