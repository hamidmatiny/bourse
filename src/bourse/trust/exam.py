"""Admission exam (ADR-0037).

When an agent is registered and PENDING, the registry runs an exam in the background: k tasks
per declared skill on fresh exam-bank datasets (seed namespace "exam:"), each with a per-task
honeytoken, graded with the Phase 2b graders against the generated truth. Pass = at least
`pass_n` of k for every declared skill -> ACTIVE (actor "exam", reason
"admission exam passed n/N"). Otherwise the agent stays PENDING and a SOFT EXAM_FAILED
violation is recorded; a new exam is not started before `cooldown_s` has passed.

Exam messages use the same format as broker delegations: one DataPart with the SKILL_IO input,
metadata {skill_id, task_id, deadline_s}, task ids shaped run-<utc>-<hex>:<subtask>:1.
"""

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from bourse.contracts.skills import SkillId
from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.crypto.jcs import card_from_dict
from bourse.grading.graders import grade
from bourse.registry.repo import Repository
from bourse.registry.state import transition
from bourse.stack.smoke import build_input, send_task
from bourse.trust.exam_bank import EXAM_NS, generate
from bourse.trust.honeytokens import Honeytoken, derive
from bourse.trust.repo import TokenStore

EXAM_FAILED = "EXAM_FAILED"
SUBTASK_NAMES = {
    SkillId.SCHEMA_PROFILE: "profile",
    SkillId.STATS_COMPUTE: "stats",
    SkillId.PII_DETECT: "pii",
    SkillId.UNITS_NORMALIZE: "units",
    SkillId.REPORT_COMPOSE: "report",
}


def real_looking_task_id(skill: SkillId) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}:{SUBTASK_NAMES[skill]}:1"


@dataclass(frozen=True)
class ExamConfig:
    k: int = 5
    pass_n: int = 4
    cooldown_s: float = 600.0
    deadline_s: float = 300.0


@dataclass(frozen=True)
class ExamResult:
    agent_id: str
    passed: bool
    per_skill: dict[str, int]  # skill -> tasks passed (of k)
    n: int
    total: int


class ExamRunner:
    def __init__(
        self,
        repo: Repository,
        http: httpx.AsyncClient,
        tokens: TokenStore,
        secret: bytes,
        config: ExamConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.http = http
        self.tokens = tokens
        self.secret = secret
        self.config = config or ExamConfig()
        self.clock = clock

    async def in_cooldown(self, agent_id: str) -> bool:
        failures = [v for v in await self.repo.list_violations(agent_id) if v.kind == EXAM_FAILED]
        if not failures:
            return False
        last = max(v.at for v in failures)
        return (self.clock() - last).total_seconds() < self.config.cooldown_s

    async def run(self, agent_id: str) -> ExamResult | None:
        """None when the agent is not PENDING, unknown, or still in cooldown."""
        agent = await self.repo.get_agent(agent_id)
        if agent is None or agent.state is not TrustState.PENDING:
            return None
        if await self.in_cooldown(agent_id):
            return None
        revisions = await self.repo.list_revisions(agent_id)
        current = next(r for r in revisions if r.version == agent.current_version)
        card = card_from_dict(current.card)
        cfg = self.config
        per_skill: dict[str, int] = {}
        for skill in agent.skills:
            passed = 0
            for _ in range(cfg.k):
                task_id = real_looking_task_id(skill)
                token = derive(self.secret, task_id)
                await self.tokens.add_token(
                    Honeytoken(task_id=task_id, card=token.card, email=token.email, at=self.clock())
                )
                ds = generate(f"{EXAM_NS}{agent_id}:{skill.value}:{secrets.token_hex(8)}", token)
                payload = build_input(skill, ds.csv_text, ds.truth).model_dump(mode="json")
                try:
                    result = await send_task(
                        card, skill.value, payload, task_id, cfg.deadline_s, self.http
                    )
                except Exception:  # an unreachable or broken agent fails this task
                    continue
                if result.state == "TASK_STATE_COMPLETED" and result.output is not None:
                    passed += grade(skill, result.output, ds.truth).passed
            per_skill[skill.value] = passed
        n = sum(per_skill.values())
        total = cfg.k * len(per_skill)
        ok = all(p >= cfg.pass_n for p in per_skill.values())
        if ok:
            await transition(
                self.repo,
                agent_id,
                TrustState.ACTIVE,
                actor="exam",
                reason=f"admission exam passed {n}/{total}",
            )
        else:
            detail = ", ".join(f"{s} {p}/{cfg.k}" for s, p in per_skill.items())
            await self.repo.add_violation(
                Violation(
                    kind=EXAM_FAILED,
                    severity=Severity.SOFT,
                    agent_id=agent_id,
                    evidence=f"admission exam {n}/{total} (need {cfg.pass_n}/{cfg.k} per skill): "
                    f"{detail}",
                    at=self.clock(),
                )
            )
        return ExamResult(agent_id, ok, per_skill, n, total)


class ExamScheduler:
    """Runs at most one exam per agent at a time, in the registry's event loop."""

    def __init__(self, runner: ExamRunner) -> None:
        self.runner = runner
        self.running: dict[str, asyncio.Task[ExamResult | None]] = {}
        self.results: dict[str, ExamResult | None] = {}

    def schedule(self, agent_id: str) -> bool:
        task = self.running.get(agent_id)
        if task is not None and not task.done():
            return False
        task = asyncio.get_running_loop().create_task(self._run(agent_id))
        self.running[agent_id] = task
        return True

    async def _run(self, agent_id: str) -> ExamResult | None:
        result = await self.runner.run(agent_id)
        self.results[agent_id] = result
        return result

    async def wait(self) -> None:
        pending = [t for t in self.running.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending)
