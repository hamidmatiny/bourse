"""Hermetic end-to-end test of the Phase 5c state machine (gate before any live run).

Real: Postgres (SqlRepository, migrations), registry app + TrustEngine, ExamRunner +
ExamScheduler, LangGraph broker + Delegator + spot checks + canaries, host reinstatement flow
(`host_reinstate`, the code behind `bourse trust reinstate`), SlackApprover decision logic.
Fake: planner LLM (fake meter), honest agents, a scripted sleeper, and the Slack transport
(FakeSlackClient) driven by a scripted clicker. No Slack network, no Ollama.

The clicker is a test-only Approver; it routes every click through SlackApprover.handle_action,
so the production trust boundary (only BOURSE_SLACK_APPROVERS ids decide) is exercised as-is.

Covers: (a) quarantine at k=3; (g) reinstate exam fail -> no approval requested;
(e) deny -> stays QUARANTINED; (f) timeout -> EXPIRED, stays QUARANTINED;
(b) exam pass -> approve -> rel_epoch 0->1, strikes 0; (c) 1-2 new strikes -> ACTIVE;
(d) 3 new strikes -> QUARANTINED, first_bad + detect_delay resolved, exposure = real + canaries.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bourse.adversaries.behaviors import PREFIX_BEFORE_DEVIATE
from bourse.adversaries.runner import _check_invariants, _finish, _first_bad_from_log
from bourse.agents.common.executor import ParsedRequest
from bourse.approval.slack import FakeSlackClient, SlackApprover
from bourse.broker.graph import Broker
from bourse.config import get_settings
from bourse.contracts.approval import ApprovalDecision, ApprovalRequest
from bourse.contracts.trust import Severity, TrustState
from bourse.registry.app import create_app
from bourse.registry.repo_sql import SqlRepository
from bourse.trust.exam import ExamConfig, ExamRunner, ExamScheduler
from bourse.trust.reinstate_client import HostReinstateResult, host_reinstate
from bourse.trust.reliability_epoch import reliability_epoch
from tests.broker_harness import (
    ADMIN,
    REGISTRY,
    BrokerBed,
    Outcome,
    honest,
    intake,
    one_agent_per_skill,
)

pytestmark = pytest.mark.db

URL = get_settings().bourse_database_url
ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "vendors",
    "agents",
    "card_revisions",
    "violations",
    "state_transitions",
    "violation_aggregates",
)
SLEEPER = "a-pii@vendor-x.local"
HEADERS = {"Authorization": f"Bearer {ADMIN}"}
K = 3
MAX_RUNS = 12
EXAM_TASKS = 5


class Sleeper:
    """Scripted turned sleeper: honest for `honest_left` executions, then drops all PII."""

    def __init__(self) -> None:
        self.honest_left = 0
        self.log: list[dict[str, Any]] = []

    def reset(self, honest_executions: int) -> None:
        self.honest_left = honest_executions
        self.log.clear()

    async def __call__(self, req: ParsedRequest) -> Outcome:
        out = await honest(req)
        deviate = self.honest_left <= 0
        if not deviate:
            self.honest_left -= 1
        self.log.append(
            {
                "task_id": req.task_id,
                "kind": "task",
                # Scripted prefix: deviations only after the honest prefix was spent.
                "prefix_before": PREFIX_BEFORE_DEVIATE,
                "deviated": deviate,
            }
        )
        return Outcome(output={"findings": []}) if deviate else out


class ClickApprover:
    """Test-only Approver: production SlackApprover + FakeSlackClient + scripted click."""

    def __init__(self, mode: str) -> None:
        assert mode in ("approve", "deny", "none")
        self.mode = mode
        self.fake = FakeSlackClient()
        self.lines: list[str] = []
        self.slack = SlackApprover(
            channel_id="C_TEST",
            approver_ids=frozenset({"U_OPERATOR"}),
            client=self.fake,
            echo=self.lines.append,
        )
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        self.requests.append(req)
        task = asyncio.create_task(self.slack.request(req, timeout_s))
        if self.mode != "none":
            # Poll the in-memory transport: FakeSlackClient has no post notification hook.
            for _ in range(1000):
                if self.fake.messages:
                    break
                await asyncio.sleep(0.005)
            buttons = self.fake.messages[-1]["blocks"][-1]["elements"]
            await self.slack.handle_action(
                request_id=str(buttons[0]["value"]),
                user_id="U_OPERATOR",
                approved=self.mode == "approve",
            )
        return await task


def _alembic() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", URL)
    return cfg


@pytest.fixture(scope="module")
def migrated() -> Iterator[None]:
    command.upgrade(_alembic(), "head")
    yield


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def engine(migrated: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(URL)
    await _truncate(eng)
    yield eng
    await _truncate(eng)
    await eng.dispose()


async def _soft_strikes(repo: SqlRepository, epoch: int) -> int:
    return sum(
        1
        for v in await repo.list_violations(SLEEPER)
        if v.severity is Severity.SOFT and v.rel_epoch == epoch
    )


async def _state(repo: SqlRepository) -> tuple[TrustState, int]:
    agent = await repo.get_agent(SLEEPER)
    assert agent is not None
    return agent.state, reliability_epoch(agent.tags)


async def _run_until_quarantine(
    bed: BrokerBed, repo: SqlRepository, epoch: int
) -> tuple[Broker, list[tuple[int, TrustState]]]:
    """Fresh broker session (fresh delivery log); e2e runs until the sleeper is QUARANTINED."""
    broker = bed.broker()
    history: list[tuple[int, TrustState]] = []
    for _ in range(MAX_RUNS):
        await broker.start(intake())
        state, _ = await _state(repo)
        history.append((await _soft_strikes(repo, epoch), state))
        if state is TrustState.QUARANTINED:
            break
    return broker, history


def _row(broker: Broker, sleeper: Sleeper, strikes: int) -> Any:
    sent = [d for d in broker.delegator.deliveries if d.agent_id == SLEEPER]
    canaries = sum(d.kind == "canary" for d in sent)
    first_bad = _first_bad_from_log([d.task_id for d in sent], sleeper.log)
    row = _finish(
        "A5a",
        "sleeper",
        "SPOTCHECK_FAIL",
        "CANARY_FAIL/SPOTCHECK_FAIL",
        delegations=len(sent),
        mode="sole-provider",
        first_bad_delegation=first_bad,
        exposure_at_quarantine=len(sent),
        strikes=strikes,
        quarantined=True,
        quarantine_severity="SOFT",
        exposure_real=len(sent) - canaries,
        canaries=canaries,
    )
    lines: list[str] = []
    _check_invariants(
        row,
        k=K,
        quarantined=True,
        registration_rejected=False,
        behavior_events=sleeper.log,
        echo=lines.append,
    )
    assert lines[-1].endswith("ok"), lines
    return row


async def _reinstate(bed: BrokerBed, approver: Any, *, timeout_s: float) -> HostReinstateResult:
    return await host_reinstate(
        bed.http,
        REGISTRY,
        HEADERS,
        SLEEPER,
        approver,
        reason="hermetic 5c",
        timeout_s=timeout_s,
        echo=lambda _l: None,
        poll_s=0.01,
        poll_timeout_s=60.0,
    )


async def _quarantine_strikes(repo: SqlRepository, epoch: int) -> int:
    """SOFT strikes of `epoch` stored up to (and including) the quarantining transition."""
    trs = [t for t in await repo.list_transitions(SLEEPER) if t.to_state is TrustState.QUARANTINED]
    q_at = max(t.at for t in trs if t.from_state is TrustState.ACTIVE)
    return sum(
        1
        for v in await repo.list_violations(SLEEPER)
        if v.severity is Severity.SOFT and v.rel_epoch == epoch and v.at <= q_at
    )


async def test_phase5c_state_machine(engine: AsyncEngine) -> None:
    started = time.monotonic()
    sleeper = Sleeper()
    repo = SqlRepository(engine)
    bed = await BrokerBed(one_agent_per_skill(a_pii=sleeper), canary_every=2, repo=repo).setup()
    runner = ExamRunner(
        repo,
        bed.http,
        bed.tokens,
        b"hermetic-5c-exam-secret",
        ExamConfig(k=EXAM_TASKS, pass_n=4, deadline_s=10, infra_backoff_s=0.0),
        echo=lambda _l: None,
    )
    sched = ExamScheduler(runner, max_concurrent=1, echo=lambda _l: None)
    bed.transport.apps["registry.test"] = httpx.ASGITransport(
        app=create_app(bed.registry_service, ADMIN, sched)
    )
    try:
        # (a) honest prefix 3, then deviate -> 3 SOFT strikes -> QUARANTINED (epoch 0).
        sleeper.reset(honest_executions=3)
        broker, history = await _run_until_quarantine(bed, repo, epoch=0)
        assert history[-1][1] is TrustState.QUARANTINED, history
        assert await _quarantine_strikes(repo, 0) == K
        q = [
            t for t in await repo.list_transitions(SLEEPER) if t.to_state is TrustState.QUARANTINED
        ]
        assert f"{K} SOFT strikes" in q[-1].reason
        _row(broker, sleeper, K)

        # (g) turned sleeper fails the reinstate exam -> no approval requested.
        never = ClickApprover("approve")
        res = await _reinstate(bed, never, timeout_s=5.0)
        assert res.status == "exam_failed" and res.exam is not None
        assert res.exam.n == 0 and res.exam.total == EXAM_TASKS
        assert never.requests == [] and never.fake.messages == []
        assert await _state(repo) == (TrustState.QUARANTINED, 0)

        # (e) exam pass -> Deny -> stays QUARANTINED, audit carries request_id + action.
        sleeper.reset(honest_executions=EXAM_TASKS)
        deny = ClickApprover("deny")
        res = await _reinstate(bed, deny, timeout_s=5.0)
        assert res.status == "denied" and res.decision is not None
        assert (res.decision.action, res.decision.request_id) == ("deny", res.request_id)
        assert await _state(repo) == (TrustState.QUARANTINED, 0)
        audit = (await repo.list_transitions(SLEEPER))[-1]
        assert audit.approval is not None and audit.approval.request_id == res.request_id
        assert "DENIED" in deny.fake.updates[-1]["text"]

        # (f) exam pass -> no click -> timeout -> EXPIRED, stays QUARANTINED.
        sleeper.reset(honest_executions=EXAM_TASKS)
        idle = ClickApprover("none")
        res = await _reinstate(bed, idle, timeout_s=0.2)
        assert res.status == "denied" and res.decision is not None
        assert (res.decision.reason, res.decision.action) == ("timeout", "timeout")
        assert "EXPIRED" in idle.fake.updates[-1]["text"]
        assert await _state(repo) == (TrustState.QUARANTINED, 0)

        # (b) exam pass -> Approve -> ACTIVE, rel_epoch 0 -> 1, strike count 0 in epoch 1.
        sleeper.reset(honest_executions=EXAM_TASKS + 2)
        ok = ClickApprover("approve")
        res = await _reinstate(bed, ok, timeout_s=5.0)
        assert res.status == "active", res
        assert (res.epoch_before, res.epoch_after) == (0, 1)
        assert await _state(repo) == (TrustState.ACTIVE, 1)
        assert await _soft_strikes(repo, 1) == 0
        assert await _soft_strikes(repo, 0) >= K  # old-epoch strikes still on record

        # (c) + (d) same identity, no re-registration: re-catch in epoch 1.
        sleeper.log.clear()
        broker, history = await _run_until_quarantine(bed, repo, epoch=1)
        partial = [(n, st) for n, st in history if 1 <= n < K]
        assert partial, history  # (c) observed 1-2 strikes ...
        assert all(st is TrustState.ACTIVE for _n, st in partial), history  # ... still ACTIVE
        assert history[-1][1] is TrustState.QUARANTINED, history  # (d)
        assert await _quarantine_strikes(repo, 1) == K
        row = _row(broker, sleeper, K)
        assert row.first_bad_delegation != "-" and row.detection_delay != "-"
        assert row.exposure == row.exposure_real + row.canaries
        assert row.canaries >= 1, row  # canaries are part of exposure, not just e2e
        assert await _state(repo) == (TrustState.QUARANTINED, 1)
    finally:
        for task in sched.running.values():
            task.cancel()
        await bed.aclose()
    assert time.monotonic() - started < 120.0
