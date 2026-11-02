"""Compose-mode acceptance (ADR-0034/0035/0037). Excluded by default and in CI.

Needs: `docker compose build`, `bourse stack up --mode compose`, `bourse stack register`
and `bourse stack wait-active`. Queries the compose database (bourse_compose) on 55432.
"""

import subprocess
from datetime import UTC, datetime

import anyio
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from bourse.config import get_settings
from bourse.stack.topology import COMPOSE_DB, PREFIX

pytestmark = pytest.mark.compose

SETTINGS = get_settings()
REGISTRY = SETTINGS.bourse_registry_url.rstrip("/")


def compose_db_url() -> str:
    return (
        make_url(SETTINGS.bourse_database_url)
        .set(database=COMPOSE_DB)
        .render_as_string(hide_password=False)
    )


def test_five_agents_active_via_exam_without_manual_activation() -> None:
    agents = httpx.get(f"{REGISTRY}/agents", timeout=10).json()
    assert len(agents) == 5
    for a in agents:
        transitions = httpx.get(f"{REGISTRY}/agents/{a['agent_id']}/transitions", timeout=10).json()
        reasons = [(t["actor"], t["reason"]) for t in transitions]
        print(a["agent_id"], a["state"], reasons)
        assert a["state"] == "ACTIVE"
        assert reasons[-1][0] == "exam" and reasons[-1][1].startswith("admission exam passed ")
        assert all(actor != "admin" for actor, _ in reasons)


async def test_egress_probe_events_attributed() -> None:
    started = datetime.now(UTC)
    proc = await anyio.run_process(
        ["docker", "compose", "--profile", "test", "run", "--rm", "egress-probe"], check=False
    )
    print(proc.stdout[-500:], proc.stderr[-500:])
    assert proc.returncode == 0
    engine = create_async_engine(compose_db_url())
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT src_ip, agent_id, host, port FROM egress_events "
                        "WHERE at >= :t ORDER BY id"
                    ),
                    {"t": started},
                )
            ).all()
    finally:
        await engine.dispose()
    print(rows)
    probe = [r for r in rows if r.src_ip == PREFIX + "70"]
    attributed = [r for r in probe if r.agent_id == "egress-probe"]
    print(f"egress events attributed {len(attributed)}/2")
    assert sorted(r.port for r in attributed) == [80, 443]
    assert all(r.host == "attacker.example" for r in attributed)


def test_e2e_inside_compose() -> None:
    proc = subprocess.run(
        ["docker", "compose", "run", "--rm", "broker", "bourse", "e2e", "all", "--repeat", "1"],
        capture_output=True,
        text=True,
        timeout=900,
    )
    print(proc.stdout[-3000:])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "runs succeeded 3/3" in proc.stdout
