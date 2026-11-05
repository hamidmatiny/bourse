"""FastAPI surface for the compose approver (no Socket Mode construction here)."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from bourse.approval.base import Approver
from bourse.approval.slack import SlackApprover
from bourse.contracts.approval import ApprovalDecision, ApprovalRequest


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ApprovalRequest
    timeout_s: float = Field(gt=0, le=86_400)


def create_app(
    *,
    approver: Approver,
    service_key: str,
    status_fn: Callable[[], dict[str, Any]],
) -> FastAPI:
    """HTTP API only. Caller owns Socket Mode lifecycle on the shared event loop."""
    app = FastAPI(title="BOURSE approver", version="0.1.0")

    def require_key(authorization: Annotated[str | None, Header()] = None) -> None:
        if not service_key:
            raise HTTPException(503, "approver key not configured")
        presented = (authorization or "").encode()
        if not hmac.compare_digest(presented, f"Bearer {service_key}".encode()):
            raise HTTPException(401, "approver bearer token required")

    @app.get("/health")
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Body always has slack status; HTTP 200 only when slack == connected."""
        body = status_fn()
        code = 200 if body.get("slack") == "connected" else 503
        return JSONResponse(body, status_code=code)

    @app.post("/v1/request", dependencies=[Depends(require_key)])
    async def request_approval(body: RequestBody) -> ApprovalDecision:
        if isinstance(approver, SlackApprover) and approver.slack_status == "error":
            return ApprovalDecision(
                approved=False,
                by="slack",
                at=body.request.created_at,
                reason="slack_unreachable",
            )
        return await approver.request(body.request, body.timeout_s)

    return app
