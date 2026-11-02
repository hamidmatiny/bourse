"""Continuous canaries (ADR-0037).

After every real delegation the broker checks a deterministic schedule: when the number of
real (kind="task") ledger rows for (agent, skill) reaches a multiple of `every` and fewer
canaries than due have been sent, one canary is sent to that agent. A canary is an exam-bank
dataset (seed namespace "exam:") with a per-task honeytoken, sent in exactly the real message
format with a real-looking task id; its output is spot-checked and graded against the
generated truth, and the ledger row (kind="canary") counts in reliability. Two consecutive
canary failures for (agent, skill) record a SOFT CANARY_FAIL violation.
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from bourse.broker.delegate import Delegator, RegistryClient
from bourse.contracts.broker import DelegationState, LedgerRow
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.contracts.trust import Severity, ViolationReport
from bourse.grading.graders import grade
from bourse.ledger.recorder import LedgerRecorder, agent_major
from bourse.ledger.repo import LedgerRepository
from bourse.stack.smoke import build_input
from bourse.trust.exam import SUBTASK_NAMES
from bourse.trust.exam_bank import EXAM_NS, generate
from bourse.trust.honeytokens import Honeytoken, derive
from bourse.trust.repo import TokenStore

CANARY_FAIL = "CANARY_FAIL"


def canary_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def canary_ok(row: LedgerRow) -> bool:
    return row.state is DelegationState.COMPLETED and row.spot_check_passed is True


class CanaryRunner:
    def __init__(
        self,
        *,
        ledger: LedgerRepository,
        recorder: LedgerRecorder,
        delegator: Delegator,
        registry: RegistryClient,
        tokens: TokenStore,
        secret: bytes,
        every: int,
        run_id: Callable[[], str] = canary_run_id,
    ) -> None:
        self.ledger = ledger
        self.recorder = recorder
        self.delegator = delegator
        self.registry = registry
        self.tokens = tokens
        self.secret = secret
        self.every = every
        self.run_id = run_id

    async def due(self, agent_id: str, skill: SkillId) -> bool:
        rows = [r for r in await self.ledger.list_rows(skill=skill) if r.agent_id == agent_id]
        real = sum(r.kind == "task" for r in rows)
        sent = sum(r.kind == "canary" for r in rows)
        return self.every > 0 and real > 0 and sent < real // self.every

    async def after_delegation(self, agent: AgentRecord, skill: SkillId) -> LedgerRow | None:
        if not await self.due(agent.agent_id, skill):
            return None
        return await self.send(agent, skill)

    async def send(self, agent: AgentRecord, skill: SkillId) -> LedgerRow:
        run_id = self.run_id()
        sub = Subtask(id=SUBTASK_NAMES[skill], skill=skill, input_ref="dataset")
        task_id = f"{run_id}:{sub.id}:1"
        token = derive(self.secret, task_id)
        await self.tokens.add_token(
            Honeytoken(task_id=task_id, card=token.card, email=token.email, at=datetime.now(UTC))
        )
        ds = generate(
            f"{EXAM_NS}canary:{agent.agent_id}:{skill.value}:{secrets.token_hex(8)}", token
        )
        payload = build_input(skill, ds.csv_text, ds.truth)
        record, output, _detail, spot = await self.delegator.attempt(run_id, sub, agent, 1, payload)
        graded = output is not None and grade(skill, output, ds.truth).passed
        row = await self.recorder.record(
            record,
            major=agent_major(agent.current_version),
            spot_check_passed=bool(spot) and graded if spot is not None else None,
            kind="canary",
        )
        canaries = [
            r
            for r in await self.ledger.list_rows(skill=skill)
            if r.agent_id == agent.agent_id and r.kind == "canary"
        ]
        if len(canaries) >= 2 and not canary_ok(canaries[-1]) and not canary_ok(canaries[-2]):
            await self.registry.report_violation(
                agent.agent_id,
                ViolationReport(
                    kind=CANARY_FAIL,
                    severity=Severity.SOFT,
                    evidence=f"2 consecutive canary failures on {skill.value}; last run {run_id}",
                ),
            )
        return row
