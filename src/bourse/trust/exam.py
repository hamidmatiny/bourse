"""Admission exam (ADR-0037).

When an agent is registered and PENDING, the registry runs an exam in the background: k tasks
per declared skill on fresh exam-bank datasets (seed namespace "exam:"), each with a per-task
honeytoken, graded with the Phase 2b graders against the generated truth. Pass = at least
`pass_n` of k for every declared skill -> ACTIVE (actor "exam", reason
"admission exam passed n/N"). Otherwise the agent stays PENDING and a SOFT EXAM_FAILED
violation is recorded; a new exam is not started before `cooldown_s` has passed.

Exam messages use the same format as broker delegations: one DataPart with the SKILL_IO input,
metadata {skill_id, task_id, deadline_s}, task ids shaped run-<utc>-<hex>:<subtask>:1.

The whole exam is bounded by `exam_timeout_s`. A task that exceeds the remaining wall budget
counts as failed with reason EXAM_TIMEOUT (logged per task).

Per-task deadline = max(deadline_s, deadline_by_model[model]) where deadline_by_model comes from
`bourse probe --latency` as 3 * p95_total_s (ADR-0044). Tasks for one agent run with concurrency 1;
exams for different agents may overlap (meter global gate = OLLAMA_NUM_PARALLEL).
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from bourse.broker.delegate import MeterBudgetClient
from bourse.contracts.exam import ExamReport, ExamTaskOutcome
from bourse.contracts.metering import TaskBudget
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

log = logging.getLogger(__name__)

EXAM_FAILED = "EXAM_FAILED"
EXAM_TIMEOUT = "EXAM_TIMEOUT"
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
    deadline_s: float = 300.0  # base; raised per model via deadline_by_model
    exam_timeout_s: float = 900.0
    # model -> max(base, 3 * p95_total_s) from probe --latency (ADR-0044)
    deadline_by_model: Mapping[str, float] = field(default_factory=dict)

    def task_deadline_s(self, model: str) -> float:
        return max(float(self.deadline_s), float(self.deadline_by_model.get(model, 0.0)))


# Back-compat alias: ExamResult is the persisted/API report shape.
ExamResult = ExamReport


class ExamRunner:
    def __init__(
        self,
        repo: Repository,
        http: httpx.AsyncClient,
        tokens: TokenStore,
        secret: bytes,
        config: ExamConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        budgets: MeterBudgetClient | None = None,
        task_max_tokens: int = 8 * 1024,
        task_max_calls: int = 20,
        model_for_agent: Callable[[str], str] | None = None,
    ) -> None:
        self.repo = repo
        self.http = http
        self.tokens = tokens
        self.secret = secret
        self.config = config or ExamConfig()
        self.clock = clock
        self.budgets = budgets
        self.task_max_tokens = task_max_tokens
        self.task_max_calls = task_max_calls
        self.model_for_agent = model_for_agent or (lambda _aid: "")
        # Concurrency 1 per agent (exams for other agents may still overlap).
        self._agent_locks: dict[str, asyncio.Lock] = {}

    async def in_cooldown(self, agent_id: str) -> bool:
        failures = [v for v in await self.repo.list_violations(agent_id) if v.kind == EXAM_FAILED]
        if not failures:
            return False
        last = max(v.at for v in failures)
        return (self.clock() - last).total_seconds() < self.config.cooldown_s

    async def run(self, agent_id: str) -> ExamResult | None:
        """None when the agent is not PENDING, unknown, or still in cooldown."""
        lock = self._agent_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            return await self._run_locked(agent_id)

    async def _run_locked(self, agent_id: str) -> ExamResult | None:
        agent = await self.repo.get_agent(agent_id)
        if agent is None or agent.state is not TrustState.PENDING:
            return None
        if await self.in_cooldown(agent_id):
            return None
        revisions = await self.repo.list_revisions(agent_id)
        current = next(r for r in revisions if r.version == agent.current_version)
        card = card_from_dict(current.card)
        cfg = self.config
        model = self.model_for_agent(agent_id)
        per_task_deadline = cfg.task_deadline_s(model)
        log.info(
            "exam start agent_id=%s model=%s task_deadline_s=%.1f (base=%.1f)",
            agent_id,
            model or "(default)",
            per_task_deadline,
            cfg.deadline_s,
        )
        per_skill: dict[str, int] = {}
        task_outcomes: list[ExamTaskOutcome] = []
        timed_out = False
        exam_deadline = time.monotonic() + cfg.exam_timeout_s
        for skill in agent.skills:
            passed = 0
            for _ in range(cfg.k):
                # One task at a time per agent (no gather); other agents may overlap.
                remaining = exam_deadline - time.monotonic()
                task_id = real_looking_task_id(skill)
                if remaining <= 0:
                    timed_out = True
                    outcome = ExamTaskOutcome(
                        skill=skill.value,
                        task_id=task_id,
                        outcome=EXAM_TIMEOUT,
                        detail="exam wall budget exhausted",
                    )
                    task_outcomes.append(outcome)
                    log.info(
                        "exam task agent_id=%s skill=%s task_id=%s outcome=%s detail=%s",
                        agent_id,
                        skill.value,
                        task_id,
                        EXAM_TIMEOUT,
                        outcome.detail,
                    )
                    continue
                token = derive(self.secret, task_id)
                await self.tokens.add_token(
                    Honeytoken(task_id=task_id, card=token.card, email=token.email, at=self.clock())
                )
                ds = generate(f"{EXAM_NS}{agent_id}:{skill.value}:{secrets.token_hex(8)}", token)
                payload = build_input(skill, ds.csv_text, ds.truth).model_dump(mode="json")
                task_deadline = min(per_task_deadline, remaining)
                try:
                    if self.budgets is not None:
                        await self.budgets.register(
                            TaskBudget(
                                task_id=task_id,
                                max_tokens=self.task_max_tokens,
                                max_calls=self.task_max_calls,
                            )
                        )
                    async with asyncio.timeout(task_deadline + 5.0):
                        result = await send_task(
                            card,
                            skill.value,
                            payload,
                            task_id,
                            task_deadline,
                            self.http,
                            bourse_kind="exam",
                        )
                except TimeoutError:
                    timed_out = True
                    outcome = ExamTaskOutcome(
                        skill=skill.value,
                        task_id=task_id,
                        outcome=EXAM_TIMEOUT,
                        detail=f"task deadline {task_deadline:.0f}s",
                    )
                    task_outcomes.append(outcome)
                    log.info(
                        "exam task agent_id=%s skill=%s task_id=%s outcome=%s detail=%s",
                        agent_id,
                        skill.value,
                        task_id,
                        EXAM_TIMEOUT,
                        outcome.detail,
                    )
                    continue
                except Exception as exc:  # unreachable or broken agent fails this task
                    outcome = ExamTaskOutcome(
                        skill=skill.value,
                        task_id=task_id,
                        outcome="ERROR",
                        detail=type(exc).__name__,
                    )
                    task_outcomes.append(outcome)
                    log.info(
                        "exam task agent_id=%s skill=%s task_id=%s outcome=ERROR detail=%s",
                        agent_id,
                        skill.value,
                        task_id,
                        outcome.detail,
                    )
                    continue
                if result.state != "TASK_STATE_COMPLETED" or result.output is None:
                    a2a_bits = [
                        f"state={result.state}",
                        f"error_kind={result.error_kind or '-'}",
                        f"error={result.error[:300] if result.error else '-'}",
                    ]
                    outcome = ExamTaskOutcome(
                        skill=skill.value,
                        task_id=task_id,
                        outcome="ERROR",
                        detail="; ".join(a2a_bits),
                    )
                    task_outcomes.append(outcome)
                    log.info(
                        "exam task agent_id=%s skill=%s task_id=%s outcome=ERROR detail=%s",
                        agent_id,
                        skill.value,
                        task_id,
                        outcome.detail,
                    )
                    continue
                g = grade(skill, result.output, ds.truth)
                if g.passed:
                    passed += 1
                    outcome = ExamTaskOutcome(
                        skill=skill.value, task_id=task_id, outcome="passed", detail=g.detail
                    )
                else:
                    outcome = ExamTaskOutcome(
                        skill=skill.value,
                        task_id=task_id,
                        outcome="grader-failed",
                        detail=g.detail,
                    )
                task_outcomes.append(outcome)
                log.info(
                    "exam task agent_id=%s skill=%s task_id=%s outcome=%s detail=%s",
                    agent_id,
                    skill.value,
                    task_id,
                    outcome.outcome,
                    outcome.detail,
                )
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
            suffix = f"; {EXAM_TIMEOUT}" if timed_out else ""
            await self.repo.add_violation(
                Violation(
                    kind=EXAM_FAILED,
                    severity=Severity.SOFT,
                    agent_id=agent_id,
                    evidence=f"admission exam {n}/{total} (need {cfg.pass_n}/{cfg.k} per skill): "
                    f"{detail}{suffix}",
                    at=self.clock(),
                )
            )
        return ExamReport(
            agent_id=agent_id,
            model=model,
            passed=ok,
            per_skill=per_skill,
            n=n,
            total=total,
            tasks=tuple(task_outcomes),
        )


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
