"""Registry HTTP API (FastAPI). Every response body is a contract model."""

import asyncio
import hmac
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bourse.contracts.exam import ExamProgress, ExamReport
from bourse.contracts.registry import (
    AgentRecord,
    ApiError,
    AvailabilityRequest,
    CardRevision,
    RegisterRequest,
    RegistrationOutcome,
    ReinstateApplyRequest,
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


NO_EXAM_ACTOR = "stack-profile:no-exam"


def create_app(
    service: RegistryService,
    admin_token: str,
    exams: "ExamScheduler | None" = None,
    rate_per_min: int = 10,
    stack_profile: str = "full",
) -> FastAPI:
    """`exams`: when set, accepted PENDING registrations start an admission exam (ADR-0037).

    `stack_profile` "no-exam" (ADR-0053): accepted PENDING registrations go ACTIVE at once,
    with an audited transition by NO_EXAM_ACTOR, and no exam is scheduled.
    """
    app = FastAPI(title="BOURSE registry", version="0.1.0")
    repo = service.repo
    buckets = TokenBucket(rate_per_min)
    # Process-local selection gate (ADR-0042 sole-provider). Not TrustState; not persisted.
    unavailable: set[str] = set()
    unavailable_for: dict[SkillId, set[str]] = {}  # per-skill selection gate
    pending_activations: set[asyncio.Task[object]] = set()
    if stack_profile == "no-exam":

        async def _activate(agent_id: str) -> None:
            await transition(
                repo,
                agent_id,
                TrustState.ACTIVE,
                actor=NO_EXAM_ACTOR,
                reason="stack profile no-exam: lint and admission exam disabled (ablation)",
            )

        def _schedule_activation(agent_id: str) -> None:
            task = asyncio.get_running_loop().create_task(_activate(agent_id))
            pending_activations.add(task)
            task.add_done_callback(pending_activations.discard)

        service.on_accepted = _schedule_activation
    elif exams is not None:
        service.on_accepted = exams.schedule

    @app.get("/stack-profile")
    async def get_stack_profile() -> dict[str, str]:
        """The stack profile this registry runs under (bench preflight compares it)."""
        return {"profile": stack_profile}

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
        hidden = unavailable | (unavailable_for.get(skill, set()) if skill is not None else set())
        fetch = limit if not hidden else min(200, limit + len(hidden))
        rows = await repo.search(skill=skill, state=state, q=q, limit=fetch)
        if not hidden:
            return rows
        return [a for a in rows if a.agent_id not in hidden][:limit]

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
        gate = unavailable if body.skill is None else unavailable_for.setdefault(body.skill, set())
        if body.available:
            gate.discard(agent_id)
        else:
            gate.add(agent_id)
        out: dict[str, bool | str] = {"agent_id": agent_id, "available": body.available}
        if body.skill is not None:
            out["skill"] = body.skill.value
        return out

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

    @app.get("/exams/progress")
    async def exam_progress() -> list[ExamProgress]:
        """Running exams (agent, skill, task n/N, attempt, elapsed) and queued ones."""
        if exams is None:
            return []
        return exams.progress()

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

    @app.post("/agents/{agent_id}/reinstate-exam", status_code=202, dependencies=admin)
    async def start_reinstate_exam(agent_id: str) -> dict[str, bool]:
        """Schedule a reinstate exam for a QUARANTINED agent (ADR-0048)."""
        if exams is None:
            raise HTTPException(503, "admission exams not configured")
        agent = await repo.get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        if agent.state is TrustState.REVOKED:
            raise HTTPException(409, "REVOKED is never reinstatable")
        if agent.state is not TrustState.QUARANTINED:
            raise HTTPException(409, f"state={agent.state.value}; only QUARANTINED may reinstate")
        return {"started": exams.schedule_reinstate(agent_id)}

    @app.get("/agents/{agent_id}/reinstate-exam-result", dependencies=admin)
    async def reinstate_exam_result(agent_id: str) -> ExamReport:
        if exams is None:
            raise HTTPException(503, "admission exams not configured")
        if await repo.get_agent(agent_id) is None:
            raise HTTPException(404, "unknown agent")
        result = exams.reinstate_results.get(agent_id)
        if result is None:
            raise HTTPException(404, "no reinstate exam result yet")
        return result

    @app.post("/agents/{agent_id}/reinstate", dependencies=admin)
    async def apply_reinstate(agent_id: str, body: ReinstateApplyRequest) -> StateTransition:
        """ACTIVE after a passed reinstate exam + approved ApprovalDecision (ADR-0048)."""
        from bourse.trust.reliability_epoch import bump_rel_epoch

        if exams is None:
            raise HTTPException(503, "admission exams not configured")
        agent = await repo.get_agent(agent_id)
        if agent is None:
            raise HTTPException(404, "unknown agent")
        if agent.state is TrustState.REVOKED:
            raise HTTPException(409, "REVOKED is never reinstatable")
        if agent.state is not TrustState.QUARANTINED:
            raise HTTPException(409, f"state={agent.state.value}; only QUARANTINED may reinstate")
        report = exams.reinstate_results.get(agent_id)
        if report is None:
            raise HTTPException(409, "reinstate exam not finished")
        if not report.passed:
            raise HTTPException(409, f"reinstate exam failed {report.n}/{report.total}")
        if not body.approval.approved:
            # Audit stay-quarantined via same-state row.
            record = StateTransition(
                agent_id=agent_id,
                from_state=TrustState.QUARANTINED,
                to_state=TrustState.QUARANTINED,
                actor="reinstate",
                reason=(
                    f"reinstate denied by {body.approval.by}: {body.approval.reason}; "
                    f"exam {report.n}/{report.total}; operator reason={body.reason!r}"
                ),
                approval=body.approval,
                at=datetime.now(UTC),
            )
            await repo.save_transition(agent.model_copy(update={"updated_at": record.at}), record)
            return record
        try:
            return await transition(
                repo,
                agent_id,
                TrustState.ACTIVE,
                actor="reinstate",
                reason=(
                    f"reinstated by {body.approval.by}; exam {report.n}/{report.total}; "
                    f"operator reason={body.reason!r}"
                ),
                approval=body.approval,
                agent_updates={"tags": bump_rel_epoch(agent.tags)},
            )
        except IllegalTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
