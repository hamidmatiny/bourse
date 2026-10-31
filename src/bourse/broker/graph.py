"""Broker run graph (ADR-0027): plan -> schedule -> delegate(level) loop -> assemble -> persist.

LangGraph checkpoints the state after every node under thread_id = run_id. The delegate node
runs one dependency level, so a run killed during level k resumes at level k and never
re-delegates subtasks completed in levels < k. Graph state holds JSON-compatible values only.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

import anyio
import anyio.from_thread
import anyio.lowlevel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from bourse.broker.delegate import Delegator, RegistryClient, RunAborted, build_input
from bourse.broker.planner import PlanResult, make_plan
from bourse.broker.repo import RunRepository
from bourse.broker.select import SelectionPolicy
from bourse.contracts.broker import (
    IntakeReport,
    RunRecord,
    RunStatus,
    SubtaskResult,
    SubtaskStatus,
)
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import IntakeRequest, Subtask, TaskDAG
from bourse.llm.submit_tool import ChatClient
from bourse.meter.repo import MeterRepository
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.executor import Executor
from bourse.runtime.watchdog import Watchdog

HEARTBEAT_SLACK_S = 60.0


class RunState(TypedDict, total=False):
    run_id: str
    request: dict[str, Any]  # IntakeRequest JSON
    plan: dict[str, Any]  # TaskDAG JSON
    planner_used: str
    levels: list[list[str]]
    level: int
    results: dict[str, dict[str, Any]]  # subtask_id -> SubtaskResult JSON
    status: str
    abort_reason: str | None


def new_run_id() -> str:
    return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def dependency_levels(dag: TaskDAG) -> list[list[str]]:
    """Level = longest dependency chain above the subtask; ids sorted within a level."""
    by_id = {s.id: s for s in dag.subtasks}
    depth: dict[str, int] = {}

    def level_of(sid: str) -> int:
        if sid not in depth:
            deps = by_id[sid].depends_on
            depth[sid] = 1 + max((level_of(d) for d in deps), default=-1)
        return depth[sid]

    for sid in by_id:
        level_of(sid)
    levels: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for sid in sorted(depth):
        levels[depth[sid]].append(sid)
    return levels


def ancestors(dag: TaskDAG, sid: str) -> set[str]:
    by_id = {s.id: s for s in dag.subtasks}
    seen: set[str] = set()
    stack = list(by_id[sid].depends_on)
    while stack:
        node = stack.pop()
        if node not in seen:
            seen.add(node)
            stack.extend(by_id[node].depends_on)
    return seen


@dataclass
class Broker:
    planner_llm: Callable[[str], ChatClient]  # run_id -> router tagged with X-Bourse-Task-Id
    registry: RegistryClient
    delegator: Delegator
    policy: SelectionPolicy
    runs: RunRepository
    usage: MeterRepository
    executor: Executor
    breaker: CircuitBreaker
    checkpointer: BaseCheckpointSaver[Any]
    wall_budget_s: float
    heartbeat_timeout_s: float
    echo: Callable[[str], None] = field(default=lambda _line: None)
    _watchdog: Watchdog | None = None

    def beat(self) -> None:
        if self._watchdog is not None:
            self._watchdog.heartbeat()

    # --- nodes -------------------------------------------------------------------------------

    async def _plan(self, state: RunState) -> RunState:
        run_id = state["run_id"]
        request = IntakeRequest.model_validate(state["request"])
        existing = await self.runs.get_run(run_id)
        if existing is not None:
            # Killed after the run row was written but before this node's checkpoint: reuse
            # the stored plan instead of asking the LLM again.
            result = PlanResult(existing.plan, existing.planner_used)
        else:
            result = await make_plan(run_id, request, self.planner_llm(run_id))
            await self.runs.create_run(
                RunRecord(
                    run_id=run_id,
                    request_text=request.request_text,
                    plan=result.dag,
                    planner_used=result.planner_used,
                    status=RunStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
        detail = f" ({result.llm_error})" if result.llm_error else ""
        self.echo(
            f"plan: {len(result.dag.subtasks)} subtasks, planner={result.planner_used}{detail}"
        )
        self.beat()
        update: RunState = {
            "plan": result.dag.model_dump(mode="json"),
            "planner_used": result.planner_used,
            "results": {},
            "level": 0,
            "status": RunStatus.RUNNING.value,
            "abort_reason": None,
        }
        if self.breaker.tripped:
            update["status"] = RunStatus.ABORTED.value
            update["abort_reason"] = self.breaker.reason
        return update

    async def _schedule(self, state: RunState) -> RunState:
        return {"levels": dependency_levels(TaskDAG.model_validate(state["plan"]))}

    async def _delegate(self, state: RunState) -> RunState:
        dag = TaskDAG.model_validate(state["plan"])
        request = IntakeRequest.model_validate(state["request"])
        by_id = {s.id: s for s in dag.subtasks}
        results = {k: SubtaskResult.model_validate(v) for k, v in state["results"].items()}
        level = state["levels"][state["level"]]
        new: dict[str, SubtaskResult] = {}
        aborted: str | None = None

        async def one(sub: Subtask) -> None:
            nonlocal aborted
            try:
                new[sub.id] = await self.executor.submit_llm(
                    lambda s: self._run_subtask(state["run_id"], dag, s, request, results), sub
                )
            except RunAborted as exc:
                aborted = str(exc)
                tg.cancel_scope.cancel()

        async with anyio.create_task_group() as tg:
            for sid in level:
                tg.start_soon(one, by_id[sid])
        for sid in level:
            if sid not in new:  # cancelled after an abort
                sub = by_id[sid]
                new[sid] = SubtaskResult(
                    subtask_id=sid, skill=sub.skill, status=SubtaskStatus.ABORTED, attempts=0
                )
        for r in new.values():
            self.echo(
                f"subtask {r.subtask_id} {r.skill.value} {r.status.value} "
                f"agent={r.agent_id or '-'} attempts={r.attempts}"
                + (f" error={r.error_kind}" if r.error_kind else "")
            )
        merged = {**state["results"], **{k: v.model_dump(mode="json") for k, v in new.items()}}
        update: RunState = {"results": merged, "level": state["level"] + 1}
        if aborted is not None:
            update["status"] = RunStatus.ABORTED.value
            update["abort_reason"] = aborted
        return update

    async def _run_subtask(
        self,
        run_id: str,
        dag: TaskDAG,
        sub: Subtask,
        request: IntakeRequest,
        done: Mapping[str, SubtaskResult],
    ) -> SubtaskResult:
        blocked = [d for d in sub.depends_on if done[d].status is not SubtaskStatus.COMPLETED]
        if blocked:
            return SubtaskResult(
                subtask_id=sub.id,
                skill=sub.skill,
                status=SubtaskStatus.SKIPPED,
                attempts=0,
                error_kind=f"dependency_not_completed:{','.join(sorted(blocked))}",
            )
        if self.breaker.tripped:
            raise RunAborted(self.breaker.reason or "breaker open")
        upstream: dict[SkillId, dict[str, Any]] = {}
        for aid in ancestors(dag, sub.id):
            output = done[aid].output
            if output is not None:
                upstream[done[aid].skill] = output
        payload = build_input(sub.skill, request.dataset, upstream)
        agents = {a.agent_id: a for a in await self.registry.active_agents(sub.skill)}
        decision = self.policy.choose(sub, list(agents.values()))
        return await self.delegator.run_subtask(run_id, sub, decision, agents, payload)

    async def _assemble(self, state: RunState) -> RunState:
        dag = TaskDAG.model_validate(state["plan"])
        results = dict(state.get("results", {}))
        if state.get("status") == RunStatus.ABORTED.value:
            for sub in dag.subtasks:
                if sub.id not in results:
                    results[sub.id] = SubtaskResult(
                        subtask_id=sub.id, skill=sub.skill, status=SubtaskStatus.ABORTED, attempts=0
                    ).model_dump(mode="json")
            return {"results": results}
        ok = all(results[s.id]["status"] == SubtaskStatus.COMPLETED.value for s in dag.subtasks)
        return {"status": (RunStatus.SUCCEEDED if ok else RunStatus.FAILED).value}

    async def _persist(self, state: RunState) -> RunState:
        await self._finish(state["run_id"], RunStatus(state["status"]))
        return {}

    async def _finish(self, run_id: str, status: RunStatus) -> None:
        run = await self.runs.get_run(run_id)
        if run is None:
            return
        ended = datetime.now(UTC)
        wall_ms = (ended - run.started_at).total_seconds() * 1000
        await self.runs.finish_run(run_id, status, ended, wall_ms)

    # --- graph ---------------------------------------------------------------------------------

    def graph(self) -> Any:
        def after_plan(state: RunState) -> str:
            return "assemble" if state.get("status") == RunStatus.ABORTED.value else "schedule"

        def after_level(state: RunState) -> str:
            if state.get("status") == RunStatus.ABORTED.value:
                return "assemble"
            return "delegate" if state["level"] < len(state["levels"]) else "assemble"

        g = StateGraph(RunState)
        g.add_node("plan", self._plan)
        g.add_node("schedule", self._schedule)
        g.add_node("delegate", self._delegate)
        g.add_node("assemble", self._assemble)
        g.add_node("persist", self._persist)
        g.add_edge(START, "plan")
        g.add_conditional_edges(
            "plan", after_plan, {"assemble": "assemble", "schedule": "schedule"}
        )
        g.add_conditional_edges(
            "schedule", after_level, {"delegate": "delegate", "assemble": "assemble"}
        )
        g.add_conditional_edges(
            "delegate", after_level, {"delegate": "delegate", "assemble": "assemble"}
        )
        g.add_edge("assemble", "persist")
        g.add_edge("persist", END)
        return g.compile(checkpointer=self.checkpointer)

    # --- entry points ---------------------------------------------------------------------------

    async def start(self, request: IntakeRequest, run_id: str | None = None) -> IntakeReport:
        rid = run_id or new_run_id()
        initial: RunState = {"run_id": rid, "request": request.model_dump(mode="json")}
        return await self._invoke(rid, initial)

    async def resume(self, run_id: str) -> IntakeReport:
        """Continue from the last checkpoint; completed levels are not delegated again."""
        snapshot = await self.graph().aget_state(_config(run_id))
        if not snapshot.values:
            raise LookupError(f"no checkpoint for run {run_id}")
        return await self._invoke(run_id, None)

    async def _invoke(self, run_id: str, initial: RunState | None) -> IntakeReport:
        graph = self.graph()
        token = anyio.lowlevel.current_token()
        kill_reason: list[str] = []
        with anyio.CancelScope() as scope:

            def on_kill(reason: str) -> None:
                kill_reason.append(reason)
                anyio.from_thread.run_sync(scope.cancel, token=token)

            self._watchdog = Watchdog(self.heartbeat_timeout_s, self.wall_budget_s, on_kill)
            with self._watchdog:
                await graph.ainvoke(initial, _config(run_id))
        self._watchdog = None
        snapshot = await graph.aget_state(_config(run_id))
        state: RunState = snapshot.values
        if scope.cancelled_caught:
            await self._finish(run_id, RunStatus.ABORTED)
            state = {**state, "status": RunStatus.ABORTED.value}
            state["abort_reason"] = f"watchdog: {kill_reason[0] if kill_reason else 'killed'}"
        return await self.report(run_id, state)

    async def report(self, run_id: str, state: RunState) -> IntakeReport:
        dag = TaskDAG.model_validate(state["plan"])
        results = [SubtaskResult.model_validate(v) for v in state.get("results", {}).values()]
        order = {s.id: i for i, s in enumerate(dag.subtasks)}
        results.sort(key=lambda r: order.get(r.subtask_id, len(order)))
        usage = await self.usage.list_usage(run_id=run_id)
        run = await self.runs.get_run(run_id)
        return IntakeReport(
            run_id=run_id,
            status=RunStatus(state.get("status", RunStatus.RUNNING.value)),
            planner_used="llm" if state.get("planner_used") == "llm" else "template",
            plan=dag,
            subtasks=tuple(results),
            outputs={
                r.skill.value: r.output
                for r in results
                if r.status is SubtaskStatus.COMPLETED and r.output is not None
            },
            delegations=tuple(await self.runs.list_delegations(run_id)),
            tokens_in=sum(u.tokens_in for u in usage),
            tokens_out=sum(u.tokens_out for u in usage),
            wall_ms=(run.wall_ms or 0.0) if run is not None else 0.0,
            abort_reason=state.get("abort_reason"),
        )


def _config(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}
