"""Compose-mode acceptance (ADR-0034/0035/0037). Excluded by default and in CI.

Needs: `docker compose build`, `bourse stack up --mode compose`, `bourse stack register`
and `bourse stack wait-active`. Queries the compose database (bourse_compose) on 55432.
"""

import subprocess
import uuid
from collections.abc import Sequence
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


# Phase5 subset: A8b-raw is left out (its exam alone is 2 x 300 s deadlines; the dev6b
# segments cover it). Timeout derived from --runs: worst case, not the expected time.
PHASE5_RUNS = 1
PHASE5_ONLY = ("A4a", "A6a", "A7-raw")
PER_RUN_S = 150  # one honest e2e run, measured 70-110 s
EXAM_S = 400  # one adversary registration + exam (early stop after 2 failures)


def phase5_timeout_s(runs: int, only: Sequence[str]) -> int:
    from bourse.adversaries.catalog import BY_ID
    from bourse.adversaries.runner import EXPOSURE_SPARE_RUNS, exposure_target
    from bourse.contracts.bench import registration_gated

    runtime = [sid for sid in only if sid in BY_ID]
    e2e = min(runs, 1)  # baseline_runs=1 below
    for sid in runtime:
        if registration_gated(sid, "full"):
            continue  # rejected by lint / exam: no e2e run
        k = exposure_target(sid, "full")
        e2e += max(runs, k + EXPOSURE_SPARE_RUNS)
    return PER_RUN_S * e2e + EXAM_S * len(runtime) + 120


def _remove_broker_runs() -> None:
    """Remove every broker run container (an orphan keeps the broker's fixed IP:
    'Address already in use' for the next run)."""
    ids = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=bourse",
            "--filter",
            "label=com.docker.compose.service=broker",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)


def compose_run_broker(args: Sequence[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    """`docker compose run --rm broker ...` that never leaves an orphan: on timeout the
    client is killed and the named container is removed."""
    _remove_broker_runs()
    name = f"bourse-test-broker-{uuid.uuid4().hex[:8]}"
    try:
        return subprocess.run(
            ["docker", "compose", "run", "--rm", "--name", name, "broker", *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def compose_db_url() -> str:
    return (
        make_url(SETTINGS.bourse_database_url)
        .set(database=COMPOSE_DB)
        .render_as_string(hide_password=False)
    )


def test_five_agents_active_via_exam_without_manual_activation() -> None:
    agents = httpx.get(f"{REGISTRY}/agents", timeout=10).json()
    honest = [a for a in agents if not str(a["agent_id"]).endswith("@vendor-x.local")]
    assert len(honest) == 5
    for a in honest:
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
    # Probe hits 80/443; DNS (53) may also be logged when resolving attacker.example.
    httpish = [r for r in attributed if r.port in (80, 443)]
    print(f"egress events attributed {len(httpish)}/2 (total probe rows {len(attributed)})")
    assert sorted(r.port for r in httpish) == [80, 443]
    assert all(r.host == "attacker.example" for r in httpish)


def _purge_vendor_x_agents() -> None:
    """REVOKED is terminal; wipe vendor-x rows so phase5 can re-register.

    Append-only triggers are switched off for the purge.
    """
    engine = create_async_engine(compose_db_url())

    async def _run() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("SET session_replication_role = replica"))
            await conn.execute(
                text("DELETE FROM state_transitions WHERE agent_id LIKE '%@vendor-x.local'")
            )
            await conn.execute(
                text("DELETE FROM violations WHERE agent_id LIKE '%@vendor-x.local'")
            )
            await conn.execute(
                text("DELETE FROM card_revisions WHERE agent_id LIKE '%@vendor-x.local'")
            )
            await conn.execute(text("DELETE FROM agents WHERE vendor_domain = 'vendor-x.local'"))
            await conn.execute(text("SET session_replication_role = DEFAULT"))
        await engine.dispose()

    anyio.run(_run)


def test_attacks_phase5_fast_subset() -> None:
    """Starts adversaries profile on a clean compose stack, then runs a fast subset.

    Scenario ids must match catalog (A7-raw / A8b-raw, not stale A7 / A8b).
    """
    up = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "adversaries",
            "up",
            "-d",
            "--wait",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(up.stdout[-1000:], up.stderr[-1000:])
    assert up.returncode == 0, up.stderr[-2000:]
    _purge_vendor_x_agents()
    timeout = phase5_timeout_s(PHASE5_RUNS, PHASE5_ONLY)
    print(f"phase5 subset {','.join(PHASE5_ONLY)} runs={PHASE5_RUNS} timeout={timeout}s")
    proc = compose_run_broker(
        [
            "bourse",
            "attacks",
            "phase5",
            "--runs",
            str(PHASE5_RUNS),
            "--baseline-runs",
            "1",
            "--only",
            ",".join(PHASE5_ONLY),
        ],
        timeout,
    )
    print(proc.stdout[-4000:])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "attacks caught" in proc.stdout
    assert "attacks error 0/" in proc.stdout
    assert "honest agents quarantined 0/" in proc.stdout
    assert "INFRA NOT READY" not in proc.stdout
    assert "INVARIANT FAIL" not in proc.stdout


def test_e2e_inside_compose() -> None:
    proc = compose_run_broker(["bourse", "e2e", "all", "--repeat", "1"], 900)
    print(proc.stdout[-3000:])
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "runs succeeded 3/3" in proc.stdout
