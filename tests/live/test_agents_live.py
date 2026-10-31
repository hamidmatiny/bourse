"""One real task per agent through the running stack (`bourse stack up` first).

Run with `uv run pytest -m live`. Needs host Ollama, Postgres, and the stack's services.
"""

import time

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bourse.agents.catalog import specs_from_settings
from bourse.config import get_settings
from bourse.contracts.skills import SkillId
from bourse.meter.repo_sql import SqlMeterRepository
from bourse.stack.smoke import build_input, send_task
from tests.datasets import csv_text, truth

pytestmark = pytest.mark.live

SETTINGS = get_settings()
CASES = {
    "profiler": (SkillId.SCHEMA_PROFILE, "people"),
    "pii_precise": (SkillId.PII_DETECT, "people"),
    "pii_fast": (SkillId.PII_DETECT, "people"),
    "normalizer": (SkillId.UNITS_NORMALIZE, "measurements"),
    "reporter": (SkillId.REPORT_COMPOSE, "measurements"),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_agent_task_through_stack(name: str) -> None:
    spec = specs_from_settings(SETTINGS)[name]
    skill, ds = CASES[name]
    task_id = f"live-{name}-{int(time.time())}"
    payload = build_input(skill, csv_text(ds), truth(ds)).model_dump(mode="json")
    result = await send_task(f"http://127.0.0.1:{spec.port}", skill.value, payload, task_id, 300.0)
    print(name, result.state, f"{result.wall_s:.1f}s", result.error[:120])
    assert result.state == "TASK_STATE_COMPLETED", result.error
    engine = create_async_engine(SETTINGS.bourse_database_url)
    try:
        rows = await SqlMeterRepository(engine).list_usage(task_id=task_id)
    finally:
        await engine.dispose()
    print(
        name, "llm_usage rows", len(rows), "tokens", sum(r.tokens_in + r.tokens_out for r in rows)
    )
    assert len(rows) > 0
