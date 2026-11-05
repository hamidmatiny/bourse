"""Registry HTTP API (FastAPI). Every response body is a contract model."""

import hmac
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bourse.contracts.exam import ExamReport
from bourse.contracts.registry import (
    AgentRecord,
    ApiError,
    AvailabilityRequest,
    CardRevision,
    RegisterRequest,
    RegistrationOutcome,
    StateChangeRequest,
    StateTransition,
    Vendor,
    VendorCreate,
)
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState, Violation, ViolationReport
from bourse.registry.rate_limit import TokenBucket
from bourse.registry.repo import VendorExists
from bourse.registry.service import RegistryService
from bourse.registry.state import IllegalTransition, UnknownAgent, transition

if TYPE_CHECKING:
    from bourse.trust.exam import ExamScheduler


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(ApiError(status=status, detail=detail).model_dump(), status_code=status)


def create_app(
    service: RegistryService,
    admin_token: str,
    exams: "ExamScheduler | None" = None,
    rate_per_min: int = 10,
) -> FastAPI:
    """`exams`: when set, accepted PENDING registrations start an admission exam (ADR-0037)."""
    app = FastAPI(title="BOURSE registry", version="0.1.0")
    repo = service.repo
    buckets = TokenBucket(rate_per_min)
    # Process-local selection gate (ADR-0042 sole-provider). Not TrustState; not persisted.
    unavailable: set[str] = set()
    if exams is not None:
        service.on_accepted = exams.schedule

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return _error(422, f"invalid request: {', '.join(fields)}")

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if not admin_token:
            raise HTTPException(503, "admin token not configured")
        presented = (authorization or "").encode()
        if not hmac.compare_digest(presented, f"Bearer {admin_token}".encode()):
            raise HTTPException(401, "admin bearer token required")

    admin = [Depends(require_admin)]

    @app.post("/vendors", status_code=201, dependencies=admin)
    async def create_vendor(body: VendorCreate) -> Vendor:
        try:
            return await service.add_vendor(body)
        except VendorExists as exc:
            raise HTTPException(409, f"vendor {body.domain} exists") from exc

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client is not None else "unknown"

    def _rate_ok(ip: str, vendor: str) -> bool:
        return buckets.allow(f"ip:{ip}") and buckets.allow(f"vendor:{vendor}")

    @app.post("/agents/register", response_model=RegistrationOutcome)
    async def register(body: RegisterRequest, request: Request) -> JSONResponse:
        if not _rate_ok(_client_ip(request), body.vendor_domain):
            return _error(429, "rate limit exceeded")
        outcome = await service.register(body)
        return JSONResponse(
            outcome.model_dump(mode="json"), status_code=201 if outcome.accepted else 422
        )

    @app.post("/agents/{agent_id}/refresh", response_model=RegistrationOutcome)
    async def refresh(agent_id: str, request: Request) -> JSONResponse:
        agent = await repo.get_agent(agent_id)
        vendor = agent.vendor_domain if agent is not None else agent_id
        if not _rate_ok(_client_ip(request), vendor):
            return _error(429, "rate limit exceeded")
        outcome = await service.refresh(agent_id)
        status = 200 if outcome.accepted else 404 if outcome.agent_id is None else 422
        return JSONResponse(outcome.model_dump(mode="json"), status_code=status)

    @app.get("/agents")
    async def search(
        skill: SkillId | None = None,
        state: TrustState | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[AgentRecord]:
        fetch = limit if not unavailable else min(200, limit + len(unavailable))
        rows = await repo.search(skill=skill, state=state, q=q, limit=fetch)
        if not unavailable:
            return rows
        return [a for a in rows if a.agent_id not in unavailable][:limit]

    @app.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> AgentRecord:
        agent = await repo.get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        return agent

    @app.post("/agents/{agent_id}/availability", dependencies=admin)
    async def set_availability(agent_id: str, body: AvailabilityRequest) -> dict[str, bool | str]:
        """Mark agent unavailable for selection without changing TrustState (ADR-0042)."""
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        if body.available:
            unavailable.discard(agent_id)
        else:
            unavailable.add(agent_id)
        return {"agent_id": agent_id, "available": body.available}

    @app.get("/agents/{agent_id}/revisions")
    async def revisions(agent_id: str) -> list[CardRevision]:
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        return await repo.list_revisions(agent_id)

    @app.post("/agents/{agent_id}/violations", status_code=201, dependencies=admin)
    async def report_violation(agent_id: str, body: ViolationReport) -> Violation:
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        violation = Violation(
            kind=body.kind,
            severity=body.severity,
            agent_id=agent_id,
            evidence=body.evidence,
            at=datetime.now(UTC),
        )
        await service.engine.ingest(violation, attributed=True)
        return violation

    @app.get("/agents/{agent_id}/violations")
    async def violations(agent_id: str) -> list[Violation]:
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        return await repo.list_violations(agent_id)

    @app.get("/agents/{agent_id}/transitions")
    async def transitions(agent_id: str) -> list[StateTransition]:
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        return await repo.list_transitions(agent_id)

    @app.post("/agents/{agent_id}/exam", status_code=202, dependencies=admin)
    async def start_exam(agent_id: str) -> dict[str, bool]:
        if exams is None:
            raise HTTPException(503, "admission exams not configured")
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        return {"started": exams.schedule(agent_id)}

    @app.get("/agents/{agent_id}/exam-result")
    async def exam_result(agent_id: str) -> ExamReport:
        """Last finished admission exam (per-task outcomes + grader detail)."""
        if exams is None:
            raise HTTPException(503, "admission exams not configured")
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        result = exams.results.get(agent_id)
        if result is None:
            raise HTTPException(404, "no exam result yet")
        return result

    @app.post("/agents/{agent_id}/state", dependencies=admin)
    async def change_state(agent_id: str, body: StateChangeRequest) -> StateTransition:
        try:
            return await transition(
                repo,
                agent_id,
                body.to_state,
                actor="admin",
                reason=body.reason,
                approval=body.approval,
            )
        except UnknownAgent as exc:
            raise HTTPException(404, "unknown agent") from exc
        except IllegalTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
