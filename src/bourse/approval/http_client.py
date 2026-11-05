"""HTTP Approver client: broker/registry → compose `approver` service (ADR-0048)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from bourse.contracts.approval import ApprovalDecision, ApprovalRequest


class HttpApprover:
    """Calls the internal approver service over bourse_internal (no Slack tokens here)."""

    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        name: str = "http-approver",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self._client = client
        self._owns = client is None
        self._name = name

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.service_key}"}

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        return self._client

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        http = await self._http()
        try:
            resp = await http.post(
                f"{self.base_url}/v1/request",
                headers=self._headers(),
                json={"request": req.model_dump(mode="json"), "timeout_s": timeout_s},
                timeout=max(timeout_s + 30.0, 60.0),
            )
        except httpx.HTTPError as exc:
            return ApprovalDecision(
                approved=False,
                by=self._name,
                at=datetime.now(UTC),
                reason=f"approver_unreachable: {type(exc).__name__}",
            )
        if resp.status_code != 200:
            return ApprovalDecision(
                approved=False,
                by=self._name,
                at=datetime.now(UTC),
                reason=f"approver_http_{resp.status_code}",
            )
        return ApprovalDecision.model_validate(resp.json())
