"""In-process broker test bed: real registry app (in-memory repo), scripted A2A fake agents,
fake metering proxy, all behind one httpx transport. No sockets, no network."""

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
import respx
from a2a.helpers.proto_helpers import new_data_part, new_task_from_user_message
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from bourse.agents.common.app import build_agent_app
from bourse.agents.common.card import signed_card
from bourse.agents.common.errors import TaskRejected
from bourse.agents.common.executor import ParsedRequest, SkillExecutor
from bourse.agents.common.spec import AgentRuntime, AgentSpec
from bourse.broker.delegate import Delegator, RegistryClient
from bourse.broker.graph import Broker
from bourse.broker.planner import TEMPLATE_PLAN, PlanSubmission
from bourse.broker.repo import InMemoryRunRepository
from bourse.broker.select import (
    MeasuredConfig,
    MeasuredPolicy,
    PriorConfig,
    PriorPolicy,
    SelectionPolicy,
)
from bourse.contracts.broker import IntakeReport
from bourse.contracts.metering import BudgetSpec
from bourse.contracts.registry import RegisterRequest
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import DatasetRef
from bourse.contracts.tasks import IntakeRequest
from bourse.contracts.trust import TrustState
from bourse.crypto.jcs import card_to_dict
from bourse.ledger.prices import load_prices
from bourse.ledger.recorder import LedgerRecorder
from bourse.ledger.repo import InMemoryLedgerRepository
from bourse.llm.omniroute import OpenAICompatProvider
from bourse.llm.router import Router
from bourse.llm.submit_tool import ChatClient
from bourse.mcp_servers.usage import InMemoryToolUsageRepository
from bourse.meter.repo import InMemoryMeterRepository
from bourse.registry.app import create_app
from bourse.registry.service import RegistryPolicy
from bourse.registry.state import transition
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetLedger
from bourse.runtime.executor import LlmOnlyExecutor
from bourse.telemetry import traced_async_client
from tests.agent_harness import METER, METER_CHAT, submit_call
from tests.datasets import csv_text, truth
from tests.registry_helpers import Party, make_party, make_service

REGISTRY = "http://registry.test"
ADMIN = secrets.token_hex(16)
DATASET = "people"
TRUTH = truth(DATASET)
PRICES = load_prices(Path(__file__).resolve().parents[1] / "config" / "prices.toml")
SPEC = BudgetSpec(max_tokens_per_call=512, max_tokens_run=20_000, wall_budget_s=60.0)


def good_output(skill: SkillId) -> dict[str, Any]:
    if skill is SkillId.REPORT_COMPOSE:
        cited = [
            {"column": s["column"], "stat": stat, "value": s[stat]}
            for s in TRUTH["stats"]["stats"]
            for stat in ("mean", "min", "max")
        ]
        return {"summary": "Numeric columns summarised.", "cited": cited}
    key = {
        SkillId.SCHEMA_PROFILE: "profile",
        SkillId.STATS_COMPUTE: "stats",
        SkillId.PII_DETECT: "pii",
        SkillId.UNITS_NORMALIZE: "units",
    }[skill]
    data: dict[str, Any] = TRUTH[key]
    return data


@dataclass
class Outcome:
    kind: str = "complete"  # complete | fail | input_required | bad_artifact
    output: dict[str, Any] | None = None
    error_kind: str = "internal"
    detail: str = "scripted failure"
    sleep_s: float = 0.0


Behaviour = Callable[[ParsedRequest], Awaitable[Outcome]]


async def complete(req: ParsedRequest) -> Outcome:
    return Outcome(output=good_output(req.skill))


class FakeExecutor(SkillExecutor):
    """Real request parsing and REJECTED handling (SkillExecutor.parse); scripted results."""

    def __init__(self, spec: AgentSpec, runtime: AgentRuntime, behaviour: Behaviour) -> None:
        super().__init__(spec, runtime)
        self.behaviour = behaviour
        self.requests: list[ParsedRequest] = []

    async def run_skill(self, ctx: Any, payload: BaseModel) -> BaseModel:
        raise NotImplementedError

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None
        task = context.current_task or new_task_from_user_message(message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            req = self.parse(message)
        except TaskRejected as rej:
            await updater.reject(self._status(updater, rej.kind, rej.detail))
            return
        self.requests.append(req)
        await updater.start_work()
        outcome = await self.behaviour(req)
        if outcome.sleep_s:
            await anyio.sleep(outcome.sleep_s)
        if outcome.kind == "fail":
            await updater.failed(self._status(updater, outcome.error_kind, outcome.detail))
        elif outcome.kind == "input_required":
            await updater.requires_input(updater.new_agent_message([Part(text="need input")]))
        else:
            output = outcome.output if outcome.kind == "complete" else {"unexpected": True}
            await updater.add_artifact([new_data_part(output)], name=f"{req.skill.value}.output")
            await updater.complete()


@dataclass
class FakeAgent:
    name: str
    party: Party
    skills: tuple[SkillId, ...]
    behaviour: Behaviour = complete
    description: str = "Fake agent for broker tests."

    @property
    def host(self) -> str:
        return f"{self.name}.test"

    @property
    def card_url(self) -> str:
        return f"http://{self.host}/.well-known/agent-card.json"

    @property
    def agent_id(self) -> str:
        return f"{self.name}@{self.party.domain}"


class HostTransport(httpx.AsyncBaseTransport):
    """Routes by host to ASGI apps or a respx router; records every request."""

    def __init__(self) -> None:
        self.apps: dict[str, httpx.AsyncBaseTransport] = {}
        self.card_overrides: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        override = self.card_overrides.get(host)
        if override is not None and request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=override)
        return await self.apps[host].handle_async_request(request)

    def posts_to(self, host: str) -> int:
        return sum(r.method == "POST" and r.url.host == host for r in self.requests)


@dataclass
class BrokerBed:
    agents: list[FakeAgent]
    planner: Callable[[httpx.Request], httpx.Response] | None = None
    llm_slots: int = 4
    deadline_s: float = 20.0
    grace_s: float = 5.0
    transport: HostTransport = field(default_factory=HostTransport)
    meter_bodies: list[str] = field(default_factory=list)
    executors: dict[str, FakeExecutor] = field(default_factory=dict)
    runs: InMemoryRunRepository = field(default_factory=InMemoryRunRepository)
    usage: InMemoryMeterRepository = field(default_factory=InMemoryMeterRepository)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    checkpointer: InMemorySaver = field(default_factory=InMemorySaver)
    ledger: InMemoryLedgerRepository = field(default_factory=InMemoryLedgerRepository)
    tool_usage: InMemoryToolUsageRepository = field(default_factory=InMemoryToolUsageRepository)
    measured: MeasuredConfig | None = None  # None -> PriorPolicy (Phase 3 tests)

    async def setup(self) -> "BrokerBed":
        parties = {a.party.domain: a.party for a in self.agents}
        self.http = traced_async_client(transport=self.transport, timeout=30)
        service = await make_service(
            *parties.values(),
            client=self.http,
            policy=RegistryPolicy(dev_hosts=frozenset(a.host for a in self.agents)),
        )
        self.registry_service = service
        self.transport.apps["registry.test"] = httpx.ASGITransport(app=create_app(service, ADMIN))
        meter = respx.Router(assert_all_called=False)
        meter.post(METER_CHAT).mock(side_effect=self._meter)
        self.transport.apps["meter.test"] = httpx.MockTransport(meter.async_handler)
        for agent in self.agents:
            spec = AgentSpec(
                name=agent.name,
                framework="a2a-sdk",
                vendor_domain=agent.party.domain,
                vendor_org=agent.party.domain,
                display_name=agent.name,
                description=agent.description,
                version="1.0.0",
                skills=agent.skills,
                model="fake",
                port=0,
                mcp={},
            )
            runtime = AgentRuntime(meter_url=METER, meter_key="unused", http_client=self.http)
            executor = FakeExecutor(spec, runtime, agent.behaviour)
            self.executors[agent.name] = executor
            card = signed_card(spec, f"http://{agent.host}", agent.party.key)
            self.transport.apps[agent.host] = httpx.ASGITransport(
                app=build_agent_app(executor, card)
            )
            outcome = await service.register(
                RegisterRequest(vendor_domain=agent.party.domain, card_url=agent.card_url)
            )
            assert outcome.accepted, outcome
            assert outcome.agent_id == agent.agent_id
            await transition(
                service.repo, agent.agent_id, TrustState.ACTIVE, actor="test", reason="test"
            )
        return self

    async def _meter(self, request: httpx.Request) -> httpx.Response:
        self.meter_bodies.append(request.content.decode())
        if self.planner is None:
            return submit_call(TEMPLATE_PLAN.model_dump(mode="json"))
        return self.planner(request)

    def planner_llm(self, run_id: str) -> ChatClient:
        provider = OpenAICompatProvider(
            METER,
            "planner-model",
            "broker-key-for-tests",
            self.http,
            provider_name="meter",
            headers={"X-Bourse-Task-Id": run_id},
        )
        return Router([provider], BudgetLedger(SPEC, run_id=run_id), self.breaker)

    def broker(self, *, wall_budget_s: float = 60.0) -> Broker:
        registry = RegistryClient(self.http, REGISTRY, ADMIN)
        delegator = Delegator(
            registry,
            self.http,
            self.runs,
            self.breaker,
            deadline_s=self.deadline_s,
            grace_s=self.grace_s,
            ledger=LedgerRecorder(self.ledger, self.usage, self.tool_usage, PRICES),
        )
        policy: SelectionPolicy = (
            PriorPolicy(PriorConfig())
            if self.measured is None
            else MeasuredPolicy(self.measured, PriorConfig(), self.ledger)
        )
        broker = Broker(
            planner_llm=self.planner_llm,
            registry=registry,
            delegator=delegator,
            policy=policy,
            runs=self.runs,
            usage=self.usage,
            executor=LlmOnlyExecutor(self.llm_slots),
            breaker=self.breaker,
            checkpointer=self.checkpointer,
            wall_budget_s=wall_budget_s,
            heartbeat_timeout_s=wall_budget_s,
            ledger=self.ledger,
        )
        delegator.heartbeat = broker.beat
        return broker

    def drift(self, agent: FakeAgent, card_dict: dict[str, Any]) -> None:
        self.transport.card_overrides[agent.host] = card_dict

    async def aclose(self) -> None:
        await self.http.aclose()


def intake(request_text: str = "audit this dataset") -> IntakeRequest:
    return IntakeRequest(
        dataset=DatasetRef.from_csv(csv_text(DATASET)), request_text=request_text, budget=SPEC
    )


def plan_response(plan: PlanSubmission | dict[str, Any]) -> httpx.Response:
    args = plan.model_dump(mode="json") if isinstance(plan, PlanSubmission) else plan
    return submit_call(args)


def one_agent_per_skill(**behaviours: Behaviour) -> list[FakeAgent]:
    """Agents a-profile ... a-report, one per skill, on one vendor."""
    party = make_party("vendor-x.local")
    names = {
        SkillId.SCHEMA_PROFILE: "a-profile",
        SkillId.STATS_COMPUTE: "a-stats",
        SkillId.PII_DETECT: "a-pii",
        SkillId.UNITS_NORMALIZE: "a-units",
        SkillId.REPORT_COMPOSE: "a-report",
    }
    return [
        FakeAgent(name, party, (skill,), behaviours.get(name.replace("-", "_"), complete))
        for skill, name in names.items()
    ]


def states(report: IntakeReport) -> dict[str, str]:
    return {r.subtask_id: r.status.value for r in report.subtasks}


def signed_card_dict(agent: FakeAgent, description: str) -> dict[str, Any]:
    """The agent's card re-signed with a changed description (valid signature, new digest)."""
    spec = AgentSpec(
        name=agent.name,
        framework="a2a-sdk",
        vendor_domain=agent.party.domain,
        vendor_org=agent.party.domain,
        display_name=agent.name,
        description=description,
        version="1.0.0",
        skills=agent.skills,
        model="fake",
        port=0,
        mcp={},
    )
    return card_to_dict(signed_card(spec, f"http://{agent.host}", agent.party.key))
