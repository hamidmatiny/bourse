"""Approver protocol (ADR-0010). Every implementation must time out to deny."""

from typing import Protocol

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest


class Approver(Protocol):
    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision: ...
