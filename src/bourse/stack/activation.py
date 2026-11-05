"""Honest-agent activation wait and diagnostics (admission exams).

Wait until every registered agent is ACTIVE or has finished with EXAM_FAILED
(bounded by exam_timeout_s + 120). Do not stop at the first EXAM_FAILED. On exit,
print one table: agent, model, per-skill n/N, per-task outcomes.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

import httpx

from bourse.config import Settings, get_settings
from bourse.contracts.exam import ExamReport
from bourse.contracts.registry import AgentRecord, StateTransition
from bourse.stack.topology import COMPOSE_DB

# Extra wall after exam_timeout_s for registry to record EXAM_FAILED / ACTIVE.
EXAM_WAIT_BUFFER_S = 120.0
HEARTBEAT_S = 60.0
POLL_S = 10.0

LARGE_AGENT_SERVICES = ("agent-reporter", "agent-pii-precise")
LARGE_AGENT_ID_SUBSTR = ("reporter", "pii-precise")


def exam_wait_s(settings: Settings | None = None) -> float:
    """Wall budget for waiting on exams: whole-exam timeout + buffer."""
    s = settings or get_settings()
    return float(s.bourse_exam_timeout_s) + EXAM_WAIT_BUFFER_S


def registry_agent_rows(client: httpx.Client, base: str) -> list[tuple[str, str, str]]:
    """Return (agent_id, state, last_transition) sorted by agent_id."""
    agents = [AgentRecord.model_validate(a) for a in client.get(f"{base}/agents").json()]
    rows: list[tuple[str, str, str]] = []
    for a in sorted(agents, key=lambda x: x.agent_id):
        raw = client.get(f"{base}/agents/{a.agent_id}/transitions").json()
        ts = [StateTransition.model_validate(t) for t in raw]
        last = f"{ts[-1].actor}: {ts[-1].reason}" if ts else "-"
        rows.append((a.agent_id, a.state.value, last))
    return rows


def exam_failed_pending(
    client: httpx.Client, base: str, agent_ids: list[str]
) -> list[tuple[str, str]]:
    """PENDING agents that already have an EXAM_FAILED violation (exam finished, failed)."""
    out: list[tuple[str, str]] = []
    for aid in agent_ids:
        resp = client.get(f"{base}/agents/{aid}/violations")
        if resp.status_code != 200:
            continue
        for v in resp.json():
            if v.get("kind") == "EXAM_FAILED":
                out.append((aid, str(v.get("evidence") or "EXAM_FAILED")))
                break
    return out


def _fetch_exam_report(client: httpx.Client, base: str, agent_id: str) -> ExamReport | None:
    resp = client.get(f"{base}/agents/{agent_id}/exam-result")
    if resp.status_code != 200:
        return None
    return ExamReport.model_validate(resp.json())


def print_exam_summary_table(
    client: httpx.Client,
    base: str,
    rows: list[tuple[str, str, str]],
    echo: Any = print,
) -> None:
    """One table: agent, model, per-skill n/N, per-task outcomes (+ grader detail on fail)."""
    from bourse.agents.catalog import ROSTER_MODELS, specs_from_settings
    from bourse.registry.service import make_agent_id
    from bourse.stack.launcher import table

    settings = get_settings()
    id_to_model = {
        make_agent_id(spec.display_name, spec.vendor_domain): ROSTER_MODELS[name]
        for name, spec in specs_from_settings(settings).items()
    }
    header = ["agent", "model", "state", "per-skill", "tasks"]
    out_rows: list[list[str]] = [header]
    echo("--- exam summary ---")
    for aid, st, last in rows:
        report = _fetch_exam_report(client, base, aid)
        model = (report.model if report and report.model else id_to_model.get(aid, "?"))
        if report is None:
            skills = last if last != "-" else "(no exam result yet)"
            tasks = "-"
        else:
            k = settings.bourse_exam_k
            skills = ", ".join(f"{s} {p}/{k}" for s, p in sorted(report.per_skill.items()))
            parts: list[str] = []
            for t in report.tasks:
                if t.outcome == "passed":
                    parts.append(f"{t.skill}:{t.outcome}")
                elif t.outcome == "grader-failed":
                    parts.append(f"{t.skill}:grader-failed[{t.detail}]")
                    echo(f"  FAIL {aid} {t.task_id} {t.skill}: {t.detail}")
                else:
                    parts.append(f"{t.skill}:{t.outcome}[{t.detail}]")
                    if t.outcome != "passed":
                        echo(f"  FAIL {aid} {t.task_id} {t.skill}: {t.outcome} {t.detail}")
            tasks = "; ".join(parts) if parts else "-"
        out_rows.append([aid, model, st, skills, tasks])
    echo(table(out_rows))


def _sample_ollama_resident(
    settings: Settings, *, echo: Any | None = None, warn_roster: bool = False
) -> int | None:
    """Sum /api/ps sizes; None if Ollama is unreachable from this process.

    When warn_roster is set, print ADR-0047 warnings for loaded models outside the roster.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/ps")
            resp.raise_for_status()
            models = resp.json().get("models") or []
            if warn_roster and echo is not None:
                from bourse.runtime.resident import warn_unexpected_loaded_models

                warn_unexpected_loaded_models(models, settings.roster_models, echo=echo)
            return sum(int(m.get("size") or 0) for m in models)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def wait_agents_active(
    settings: Settings | None = None,
    timeout_s: float | None = None,
    echo: Any = print,
) -> int:
    """Poll until every agent is ACTIVE or EXAM_FAILED (exam finished).

    Returns 0 iff all ACTIVE; 1 if any EXAM_FAILED or timeout. Always prints the exam table
    when every agent has a terminal exam outcome or the wait times out. Also samples Ollama
    /api/ps each poll and prints the exam-window resident peak.
    """
    from bourse.runtime.resident import RESIDENT_LIMIT_BYTES, format_resident_peak
    from bourse.stack.launcher import table

    settings = settings or get_settings()
    timeout = exam_wait_s(settings) if timeout_s is None else float(timeout_s)
    base = settings.bourse_registry_url.rstrip("/")
    started = time.monotonic()
    last_key: tuple[tuple[str, str], ...] | None = None
    last_beat = time.monotonic()
    exam_peak = 0
    roster_warned = False
    while True:
        sampled = _sample_ollama_resident(
            settings, echo=echo, warn_roster=not roster_warned
        )
        if sampled is not None:
            exam_peak = max(exam_peak, sampled)
            roster_warned = True  # one warning pass per wait (repeat only if never sampled)
        try:
            with httpx.Client(timeout=10.0) as client:
                rows = registry_agent_rows(client, base)
                pending = [aid for aid, st, _ in rows if st == "PENDING"]
                failed = exam_failed_pending(client, base, pending)
                failed_ids = {aid for aid, _ in failed}
                still_running = [aid for aid in pending if aid not in failed_ids]
                active = sum(r[1] == "ACTIVE" for r in rows)
                key = tuple((r[0], r[1]) for r in rows)
                elapsed = time.monotonic() - started
                now = time.monotonic()
                if key != last_key:
                    echo(f"[{elapsed:5.0f}s] ACTIVE {active}/{len(rows)} pending_exams={len(still_running)}")
                    for r in rows:
                        echo(f"  {r[0]} {r[1]}")
                    last_key = key
                    last_beat = now
                elif now - last_beat >= HEARTBEAT_S:
                    echo(
                        f"[{elapsed:5.0f}s] heartbeat ACTIVE {active}/{len(rows)} "
                        f"pending_exams={len(still_running)} exam_failed={len(failed_ids)}"
                    )
                    last_beat = now
                # All exams finished: every agent ACTIVE or PENDING+EXAM_FAILED.
                if rows and not still_running:
                    echo(table([["agent", "state", "last transition"], *[list(r) for r in rows]]))
                    print_exam_summary_table(client, base, rows, echo=echo)
                    limit = settings.bourse_resident_limit_bytes or RESIDENT_LIMIT_BYTES
                    echo(f"exams {format_resident_peak(exam_peak, limit)}")
                    return 0 if active == len(rows) and not failed_ids else 1
                if elapsed >= timeout:
                    echo(table([["agent", "state", "last transition"], *[list(r) for r in rows]]))
                    echo(
                        f"wait-active timeout after {timeout:.0f}s "
                        f"(ACTIVE {active}/{len(rows)}; still running exams: {still_running})"
                    )
                    print_exam_summary_table(client, base, rows, echo=echo)
                    limit = settings.bourse_resident_limit_bytes or RESIDENT_LIMIT_BYTES
                    echo(f"exams {format_resident_peak(exam_peak, limit)}")
                    return 1
        except httpx.HTTPError as exc:
            echo(f"registry unreachable: {type(exc).__name__}")
            return 1
        time.sleep(POLL_S)


def _compose_logs(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "logs", *args],
        check=False,
        text=True,
        capture_output=True,
    )
    return (result.stdout or "") + (result.stderr or "")


def dump_activation_diagnostics(echo: Any = print) -> None:
    """Registry exam lines, large-agent logs, llm_usage on bourse_compose, Ollama /api/ps."""
    echo("=== activation diagnostics ===")
    settings = get_settings()
    base = settings.bourse_registry_url.rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            rows = registry_agent_rows(client, base)
            echo("--- agent states ---")
            for aid, st, last in rows:
                echo(f"  {aid} {st} {last}")
            print_exam_summary_table(client, base, rows, echo=echo)
            echo("--- violations ---")
            for aid, st, _ in rows:
                vios = client.get(f"{base}/agents/{aid}/violations").json()
                if not vios:
                    continue
                for v in vios:
                    echo(f"  {aid}: {v.get('kind')} {str(v.get('evidence') or '')[:180]}")
    except httpx.HTTPError as exc:
        echo(f"registry API unreachable: {type(exc).__name__}")
    reg = _compose_logs("--tail", "400", "registry")
    exam_lines = [
        ln
        for ln in reg.splitlines()
        if "exam task" in ln or "EXAM_" in ln or "admission exam" in ln.lower()
    ]
    echo("--- registry per-task exam log lines ---")
    echo("\n".join(exam_lines[-120:]) if exam_lines else "(no exam task lines yet)")
    for svc in LARGE_AGENT_SERVICES:
        echo(f"--- {svc} logs (tail 40) ---")
        echo(_compose_logs("--tail", "40", svc).rstrip() or "(empty)")
    echo(f"--- llm_usage on {COMPOSE_DB} ---")
    sql = (
        "select agent_id, status, error_kind, count(*), "
        "coalesce(max(queue_wait_ms),0) as max_queue_wait_ms "
        "from llm_usage group by 1,2,3 order by 1,2,3;"
    )
    psql = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "bourse",
            "-d",
            COMPOSE_DB,
            "-c",
            sql,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    echo((psql.stdout or psql.stderr or "").rstrip() or "(psql empty)")
    echo("--- ollama /api/ps ---")
    ps = subprocess.run(
        ["curl", "-s", "http://127.0.0.1:11434/api/ps"],
        check=False,
        text=True,
        capture_output=True,
    )
    echo((ps.stdout or ps.stderr or "").rstrip()[:2000] or "(ollama unreachable)")
    echo("=== end diagnostics ===")
