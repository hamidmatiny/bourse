"""Smoke run: every agent x skill x dataset through A2A, graded, with metered usage."""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, new_data_part
from a2a.types import Message, Role, SendMessageRequest, TaskState
from pydantic import BaseModel

from bourse.agents.catalog import specs_from_settings
from bourse.config import Settings
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import (
    SKILL_IO,
    DatasetRef,
    PiiOut,
    ReportIn,
    SchemaProfileOut,
    StatsOut,
    UnitsOut,
)
from bourse.grading.graders import grade
from bourse.meter.repo import MeterRepository

DEFAULT_DATASET_DIR = Path("tests/fixtures/datasets")
SMOKE_DIR = Path("data/smoke")
CALL_DEADLINE_S = 300.0


@dataclass
class CallResult:
    state: str
    output: dict[str, Any] | None
    error: str
    wall_s: float


async def send_task(
    agent_url: str,
    skill_id: str,
    payload_json: Any,
    task_id: str,
    deadline_s: float = CALL_DEADLINE_S,
    http_client: httpx.AsyncClient | None = None,
) -> CallResult:
    started = time.monotonic()
    owns = http_client is None
    client_http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(deadline_s + 30))
    try:
        client = await create_client(
            agent_url, ClientConfig(streaming=True, httpx_client=client_http)
        )
        message = Message(
            message_id=uuid.uuid4().hex,
            role=Role.ROLE_USER,
            parts=[new_data_part(payload_json)],
        )
        message.metadata.update(
            {"skill_id": skill_id, "task_id": task_id, "deadline_s": deadline_s}
        )
        state = "TASK_STATE_UNSPECIFIED"
        output: dict[str, Any] | None = None
        error = ""
        async for event in client.send_message(SendMessageRequest(message=message)):
            if event.HasField("status_update"):
                status = event.status_update.status
                state = TaskState.Name(status.state)
                if status.HasField("message"):
                    error = " ".join(p.text for p in status.message.parts if p.text)
            elif event.HasField("artifact_update"):
                data = get_data_parts(list(event.artifact_update.artifact.parts))
                if data and isinstance(data[0], dict):
                    output = data[0]
            elif event.HasField("task"):
                state = TaskState.Name(event.task.status.state)
    finally:
        if owns:
            await client_http.aclose()
    return CallResult(state=state, output=output, error=error, wall_s=time.monotonic() - started)


def build_input(skill: SkillId, csv_text: str, truth: dict[str, Any]) -> BaseModel:
    dataset = DatasetRef.from_csv(csv_text)
    if skill is SkillId.REPORT_COMPOSE:
        # Built from TRUTH so the reporter is graded in isolation from upstream agents.
        return ReportIn(
            dataset=dataset,
            profile=SchemaProfileOut.model_validate(truth["profile"]),
            stats=StatsOut.model_validate(truth["stats"]),
            pii=PiiOut.model_validate(truth["pii"]),
            units=UnitsOut.model_validate(truth["units"]),
        )
    return SKILL_IO[skill][0].model_validate({"dataset": dataset.model_dump()})


@dataclass
class SmokeRow:
    agent: str
    skill: str
    dataset: str
    task_id: str
    state: str
    passed: bool
    n: int
    N: int  # n/N reporting convention
    tokens_in: int
    tokens_out: int
    wall_s: float
    detail: str


async def smoke_all(
    settings: Settings,
    repo: MeterRepository,
    datasets: list[str],
    dataset_dir: Path = DEFAULT_DATASET_DIR,
    echo: Callable[[str], None] = print,
    agents: list[str] | None = None,
) -> tuple[list[SmokeRow], Path]:
    specs = specs_from_settings(settings)
    plan = [
        (name, skill, ds)
        for name, spec in specs.items()
        if agents is None or name in agents
        for skill in spec.skills
        for ds in datasets
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows: list[SmokeRow] = []
    started = time.monotonic()
    for i, (name, skill, ds) in enumerate(plan, start=1):
        csv_text = (dataset_dir / f"{ds}.csv").read_text()
        truth: dict[str, Any] = json.loads((dataset_dir / f"{ds}.truth.json").read_text())
        task_id = f"smoke-{stamp}-{name}-{skill.value}-{ds}"
        url = f"http://127.0.0.1:{specs[name].port}"
        payload = build_input(skill, csv_text, truth).model_dump(mode="json")
        result = await send_task(url, skill.value, payload, task_id)
        g = grade(skill, result.output or {}, truth) if result.output is not None else None
        usage = await repo.list_usage(task_id=task_id)
        row = SmokeRow(
            agent=name,
            skill=skill.value,
            dataset=ds,
            task_id=task_id,
            state=result.state.removeprefix("TASK_STATE_"),
            passed=bool(g and g.passed),
            n=g.n if g else 0,
            N=g.N if g else 0,
            tokens_in=sum(u.tokens_in for u in usage),
            tokens_out=sum(u.tokens_out for u in usage),
            wall_s=round(result.wall_s, 1),
            detail=g.detail if g else result.error[:200],
        )
        rows.append(row)
        elapsed = time.monotonic() - started
        eta = elapsed / i * (len(plan) - i)
        echo(
            f"[{i}/{len(plan)}] {name} {skill.value} {ds} {row.state} "
            f"passed={row.passed} {row.n}/{row.N} tokens={row.tokens_in}/{row.tokens_out} "
            f"wall={row.wall_s}s eta={eta:.0f}s"
        )
    out = SMOKE_DIR / f"smoke-{stamp}.json"
    await anyio.Path(SMOKE_DIR).mkdir(parents=True, exist_ok=True)
    await anyio.Path(out).write_text(json.dumps([asdict(r) for r in rows], indent=2) + "\n")
    return rows, out


def summary_lines(rows: list[SmokeRow]) -> list[str]:
    completed = sum(r.state == "COMPLETED" for r in rows)
    passed = sum(r.passed for r in rows)
    lines = [
        f"calls completed {completed}/{len(rows)}",
        f"graded passed {passed}/{len(rows)}",
    ]
    for skill in sorted({r.skill for r in rows}):
        subset = [r for r in rows if r.skill == skill]
        lines.append(f"{skill} passed {sum(r.passed for r in subset)}/{len(subset)}")
    lines.append(
        f"tokens in {sum(r.tokens_in for r in rows)} out {sum(r.tokens_out for r in rows)}"
    )
    return lines


def result_table(rows: list[SmokeRow]) -> list[list[str]]:
    out = [
        ["agent", "skill", "dataset", "state", "passed", "n/N", "tokens_in", "tokens_out", "wall_s"]
    ]
    for r in rows:
        out.append(
            [
                r.agent,
                r.skill,
                r.dataset,
                r.state,
                str(r.passed),
                f"{r.n}/{r.N}",
                str(r.tokens_in),
                str(r.tokens_out),
                str(r.wall_s),
            ]
        )
    return out
