import asyncio
import csv
import io
import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from bourse.contracts.registry import VerifyResult
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import SKILL_IO, DatasetRef
from bourse.crypto.card_sign import verify_card
from bourse.crypto.jcs import card_from_dict
from bourse.grading.graders import grade
from bourse.mcp_servers.table import column_stats_impl
from bourse.stack.smoke import build_input
from tests.agent_harness import (
    AgentHarness,
    CrewHandler,
    dataset_payload,
    react_response,
    submit_call,
)
from tests.datasets import csv_text, truth
from tests.unit.agents.conftest import ExternalNetworkBlocked

MEAS_NUMERIC = ["sample_id", "weight_lb", "temp_F", "dist_km", "dur_min"]


def _report_args() -> dict[str, Any]:
    s = {x["column"]: x for x in truth("measurements")["stats"]["stats"]}
    return {
        "summary": "25 samples; mean weight and max temperature are cited.",
        "cited": [
            {"column": "weight_lb", "stat": "mean", "value": s["weight_lb"]["mean"]},
            {"column": "temp_F", "stat": "max", "value": s["temp_F"]["max"]},
            {"column": "dist_km", "stat": "null_count", "value": 1},
        ],
    }


SUBMIT_ARGS: dict[str, dict[str, Any]] = {
    "profiler": {
        "dtype_overrides": {},
        "numeric_columns": MEAS_NUMERIC,
        "comment": "weight_lb mean is 999.0 and temp_F max is 999.0",
    },
    "pii_fast": {"name_columns": ["name"]},
    "normalizer": {"units": {"sample_id": None, "site": None}},
    "reporter": _report_args(),
}

# agent -> (skill, dataset) for the main valid-input case
CASES: dict[str, tuple[SkillId, str]] = {
    "profiler": (SkillId.STATS_COMPUTE, "measurements"),
    "pii_precise": (SkillId.PII_DETECT, "people"),
    "pii_fast": (SkillId.PII_DETECT, "people"),
    "normalizer": (SkillId.UNITS_NORMALIZE, "measurements"),
    "reporter": (SkillId.REPORT_COMPOSE, "measurements"),
}
AGENTS = list(CASES)
TOOL_AGENTS = {"profiler": "table", "pii_precise": "pii", "pii_fast": "pii", "normalizer": "units"}


class CrewScript:
    """ReAct replies for crewai: call submit once, then give the final answer."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.headers: list[str | None] = []
        self.hosts: list[str] = []
        self.delay_s = delay_s

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.headers.append(request.headers.get("X-Bourse-Task-Id"))
        self.hosts.append(request.url.host)
        if self.delay_s:
            time.sleep(self.delay_s)
        if len(self.headers) == 1:
            return react_response(
                "Thought: the name column holds people.\nAction: submit\n"
                'Action Input: {"name_columns": ["name"]}'
            )
        return react_response("Thought: I submitted the answer.\nFinal Answer: done")


@pytest.fixture(autouse=True)
def crewai_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CrewAI writes user data under ~; keep it inside the test's tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))


def _meter_for(name: str, delay_s: float = 0.0) -> Callable[[httpx.Request], Any] | None:
    if name == "pii_precise":
        return None
    args = SUBMIT_ARGS[name]

    async def handler(request: httpx.Request) -> httpx.Response:
        if delay_s:
            await asyncio.sleep(delay_s)
        return submit_call(args)

    return handler


def _harness(name: str, *, delay_s: float = 0.0, broken: set[str] | None = None) -> AgentHarness:
    crew: CrewHandler | None = CrewScript(delay_s) if name == "pii_precise" else None
    return AgentHarness(name, _meter_for(name, delay_s), crew=crew, broken=broken)


def _payload(name: str) -> dict[str, Any]:
    skill, ds = CASES[name]
    if skill is SkillId.REPORT_COMPOSE:
        return build_input(skill, csv_text(ds), truth(ds)).model_dump(mode="json")
    return dataset_payload(ds)


@pytest.mark.parametrize("name", AGENTS)
async def test_valid_input_completes_with_contract_artifact(name: str) -> None:
    h = _harness(name)
    skill, ds = CASES[name]
    result = await h.send(skill.value, _payload(name), task_id="task-42")
    assert result.state == "TASK_STATE_COMPLETED", result.error
    assert result.output is not None
    output = SKILL_IO[skill][1].model_validate(result.output)
    g = grade(skill, output, truth(ds))
    assert g.passed, g.detail
    if name == "pii_precise":
        crew = h.runtime.crewai_client_params
        assert crew is not None
    else:
        assert h.meter_requests, "agent made no LLM call"
        assert {r.headers["X-Bourse-Task-Id"] for r in h.meter_requests} == {"task-42"}


async def test_profiler_schema_profile_completes() -> None:
    h = _harness("profiler")
    result = await h.send(SkillId.SCHEMA_PROFILE.value, dataset_payload("people"))
    assert result.state == "TASK_STATE_COMPLETED", result.error
    g = grade(SkillId.SCHEMA_PROFILE, result.output or {}, truth("people"))
    assert g.passed, g.detail


async def test_profiler_numbers_come_from_tools_not_llm() -> None:
    h = _harness("profiler")
    result = await h.send(SkillId.STATS_COMPUTE.value, dataset_payload("measurements"))
    assert result.output is not None
    expected = column_stats_impl(csv_text("measurements")).model_dump(mode="json")
    assert result.output == expected
    assert 999.0 not in [s[k] for s in result.output["stats"] for k in ("mean", "max")]


async def test_crewai_agent_sends_task_id_and_stays_local() -> None:
    script = CrewScript()
    h = AgentHarness("pii_precise", _precise_meter({}), crew=script)
    result = await h.send(SkillId.PII_DETECT.value, dataset_payload("people"), task_id="task-77")
    assert result.state == "TASK_STATE_COMPLETED", result.error
    assert script.headers and set(script.headers) == {"task-77"}
    assert set(script.hosts) == {"meter.test"}
    # submit ends the crew (result_as_answer): one LLM call, no "Final Answer" turn.
    assert len(script.headers) == 1, script.headers
    # 1.1.0 (ADR-0052): the column-kind call goes through the agent's router, same meter/task.
    assert {r.headers["X-Bourse-Task-Id"] for r in h.meter_requests} == {"task-77"}
    assert {r.url.host for r in h.meter_requests} == {"meter.test"}


def _precise_meter(
    kinds: dict[str, str], hits: list[dict[str, Any]] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Fake meter for pii-precise's submit-tool steps: answers by the requested schema."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        props = body["tools"][0]["function"]["parameters"]["properties"]
        if "columns" in props:
            return submit_call({"columns": [{"column": c, "kind": k} for c, k in kinds.items()]})
        return submit_call({"hits": hits or []})

    return handler


async def test_pii_precise_adds_only_contact_check_accepted_findings() -> None:
    """ADR-0052: LLM-proposed findings are a union on top of the deterministic ones, and the
    deterministic contact_check rejects what is not a contact (here: `color` as PHONE)."""
    from bourse.bench.generator import generate

    case = generate("dev", "dev-agent-hard", "hard_pii", "bench-dev:agent-test:21")
    header = case.csv_text.splitlines()[0].split(",")
    kinds = {"reach": "EMAIL", "tel": "PHONE", "notes": "FREE_TEXT", "color": "PHONE"}
    kinds = {c: k for c, k in kinds.items() if c in header}
    rows = list(csv.reader(io.StringIO(case.csv_text)))[1:]
    notes_hits = []
    if "notes" in header:
        i = header.index("notes")
        notes_hits = [
            {"row": r, "kind": "PHONE"} for r, row in enumerate(rows) if "call " in row[i]
        ] + [{"row": r, "kind": "EMAIL"} for r, row in enumerate(rows) if "call " not in row[i]]
    h = AgentHarness("pii_precise", _precise_meter(kinds, notes_hits), crew=CrewScript())
    payload = {"dataset": DatasetRef.from_csv(case.csv_text).model_dump(mode="json")}
    result = await h.send(SkillId.PII_DETECT.value, payload, task_id="task-hard")
    assert result.state == "TASK_STATE_COMPLETED", result.error
    assert result.output is not None
    found = {(f["row"], f["column"], f["kind"]) for f in result.output["findings"]}
    want = {(f["row"], f["column"], f["kind"]) for f in case.truth["pii"]["findings"]}
    extra_cols = {"reach", "tel", "notes"} & set(header)
    assert extra_cols, header  # the seed was picked so the case has at least one such column
    for col in extra_cols:  # every planted contact in those columns is found ...
        assert {w for w in want if w[1] == col} <= found, col
    assert not {f for f in found if f[1] in extra_cols | {"color"}} - want  # ... and no more


def test_network_guard_blocks_external_connects(no_external_network: list[Any]) -> None:
    sock = socket.socket()
    try:
        with pytest.raises(ExternalNetworkBlocked):
            sock.connect(("203.0.113.10", 80))
    finally:
        sock.close()
    no_external_network.clear()


@pytest.mark.parametrize("name", AGENTS)
async def test_invalid_input_rejected_without_echo(name: str) -> None:
    h = _harness(name)
    skill, _ = CASES[name]
    bad = {
        "dataset": {
            "csv_text": "secret-cell-value,b\n1,2\n",
            "sha256": "0" * 64,
            "n_rows": 1,
            "n_cols": 2,
        }
    }
    result = await h.send(skill.value, bad)
    assert result.state == "TASK_STATE_REJECTED"
    assert result.error.startswith("invalid_input")
    assert "secret-cell-value" not in result.error
    assert h.meter_requests == []


@pytest.mark.parametrize("name", AGENTS)
async def test_unsupported_skill_rejected(name: str) -> None:
    h = _harness(name)
    other = next(s for s in SkillId if s not in h.spec.skills)
    for skill_id in (other.value, "web.browse"):
        result = await h.send(skill_id, _payload(name))
        assert result.state == "TASK_STATE_REJECTED"
        assert result.error.startswith("unsupported_skill")


@pytest.mark.parametrize("name", list(TOOL_AGENTS))
async def test_mcp_tool_error_fails(name: str) -> None:
    h = _harness(name, broken={TOOL_AGENTS[name]})
    skill, _ = CASES[name]
    result = await h.send(skill.value, _payload(name))
    assert result.state == "TASK_STATE_FAILED"
    assert result.error.startswith("tool_error"), result.error


@pytest.mark.parametrize("name", AGENTS)
async def test_deadline_fails(name: str) -> None:
    h = _harness(name, delay_s=3.0)
    skill, _ = CASES[name]
    started = time.monotonic()
    result = await h.send(skill.value, _payload(name), deadline_s=1.0)
    assert result.state == "TASK_STATE_FAILED"
    assert result.error.startswith("deadline"), result.error
    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize("name", AGENTS)
async def test_card_served_and_verifies(name: str) -> None:
    h = _harness(name)
    async with h.client() as http:
        resp = await http.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = card_from_dict(resp.json())
    assert verify_card(card, h.party.keys) is VerifyResult.OK
    assert [s.id for s in card.skills] == [s.value for s in h.spec.skills]
    assert card.capabilities.streaming is True
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"


async def test_reporter_wrong_citation_retries_then_fails() -> None:
    args = _report_args()
    args["cited"][0]["value"] = 12345.678
    h = AgentHarness("reporter", lambda request: submit_call(args))
    result = await h.send(SkillId.REPORT_COMPOSE.value, _payload("reporter"))
    assert result.state == "TASK_STATE_FAILED"
    assert result.error.startswith("invalid_llm_output"), result.error
    assert len(h.meter_requests) == 3  # first draft + 2 retries
    feedback = json.loads(h.meter_requests[-1].content)["messages"][-1]["content"]
    assert "weight_lb.mean is not an input value" in feedback


async def test_normalizer_llm_only_for_unresolved_headers() -> None:
    h = _harness("normalizer")
    result = await h.send(SkillId.UNITS_NORMALIZE.value, dataset_payload("measurements"))
    assert result.state == "TASK_STATE_COMPLETED"
    assert len(h.meter_requests) == 1
    prompt = json.loads(h.meter_requests[0].content)["messages"][-1]["content"]
    assert '"sample_id"' in prompt and '"site"' in prompt and "weight_lb" not in prompt
