"""Honest-agent activation wait and diagnostics (admission exams).

Wait until every registered agent is ACTIVE or has finished with EXAM_FAILED (budget computed
from the number of exam tasks, see exam_wait_budget_s). EXAM_INCONCLUSIVE keeps the agent
PENDING and does not end the wait (reschedule). On exit, print per-skill n/N plus
passed/grader-failed/infra-retried/inconclusive counts (ADR-0049).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Sequence
from typing import Any

import httpx

from bourse.config import Settings, get_settings
from bourse.contracts.exam import ExamProgress, ExamReport
from bourse.contracts.registry import AgentRecord, StateTransition
from bourse.stack.topology import COMPOSE_DB

# Extra wall after exam_timeout_s for registry to record EXAM_FAILED / ACTIVE.
EXAM_WAIT_BUFFER_S = 120.0
HEARTBEAT_S = 60.0
POLL_S = 10.0

LARGE_AGENT_SERVICES = ("agent-reporter", "agent-pii-precise")
LARGE_AGENT_ID_SUBSTR = ("reporter", "pii-precise")


def exam_wait_budget_s(
    task_counts: Sequence[int],
    *,
    task_deadline_s: float,
    exam_timeout_s: float,
    infra_retries: int,
    concurrency: int,
    buffer_s: float = EXAM_WAIT_BUFFER_S,
) -> float:
    """Worst-case wall to finish every exam, from the number of tasks per exam.

    One exam is bounded by min(tasks * (1 + retries) * (deadline + grace), hard cap) where
    hard cap = exam_timeout_s + deadline + 60 (ExamScheduler backstop). The scheduler runs
    `concurrency` exams at once, so the makespan is at most sum/concurrency + max
    (list-scheduling bound), plus a buffer for the registry to record the outcome.
    """
    from bourse.trust.exam import ExamConfig

    if not task_counts:
        return buffer_s
    cfg = ExamConfig()
    hard_cap = exam_timeout_s + task_deadline_s + cfg.hard_cap_slack_s
    per_attempt = task_deadline_s + cfg.slot_grace_s
    per_exam = [min(n * (1 + infra_retries) * per_attempt, hard_cap) for n in task_counts]
    return sum(per_exam) / max(1, concurrency) + max(per_exam) + buffer_s


def max_exam_task_deadline_s(settings: Settings) -> float:
    by_model = dict(settings.exam_deadline_by_model)
    return max([float(settings.bourse_exam_deadline_s), *by_model.values()])


def exam_wait_s(
    settings: Settings | None = None, task_counts: Sequence[int] | None = None
) -> float:
    """Wall budget for waiting on exams, computed from task counts (default: honest roster)."""
    from bourse.trust.exam import ExamConfig

    s = settings or get_settings()
    if task_counts is None:
        from bourse.agents.catalog import specs_from_settings

        task_counts = [
            s.bourse_exam_k * len(spec.skills) for spec in specs_from_settings(s).values()
        ]
    return exam_wait_budget_s(
        task_counts,
        task_deadline_s=max_exam_task_deadline_s(s),
        exam_timeout_s=float(s.bourse_exam_timeout_s),
        infra_retries=ExamConfig().infra_max_retries,
        concurrency=s.ollama_num_parallel,
    )


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


def _exam_count_line(report: ExamReport) -> str:
    """passed / grader-failed / infra-retried / inconclusive (ADR-0049)."""
    passed = sum(1 for t in report.tasks if t.outcome == "passed")
    grader_failed = sum(1 for t in report.tasks if t.outcome == "grader-failed")
    infra_retried = sum(t.infra_retries for t in report.tasks)
    inconclusive = sum(1 for t in report.tasks if t.outcome in ("INFRA", "EXAM_TIMEOUT"))
    return (
        f"passed={passed} grader-failed={grader_failed} "
        f"infra-retried={infra_retried} inconclusive={inconclusive}"
    )


def print_exam_summary_table(
    client: httpx.Client,
    base: str,
    rows: list[tuple[str, str, str]],
    echo: Any = print,
) -> None:
    """One table: agent, model, per-skill n/N, counts, per-task outcomes."""
    from bourse.agents.catalog import ROSTER_MODELS, specs_from_settings
    from bourse.registry.service import make_agent_id
    from bourse.stack.launcher import table

    settings = get_settings()
    id_to_model = {
        make_agent_id(spec.display_name, spec.vendor_domain): ROSTER_MODELS[name]
        for name, spec in specs_from_settings(settings).items()
    }
    header = ["agent", "model", "state", "per-skill", "counts", "tasks"]
    out_rows: list[list[str]] = [header]
    echo("--- exam summary ---")
    for aid, st, last in rows:
        report = _fetch_exam_report(client, base, aid)
        model = report.model if report and report.model else id_to_model.get(aid, "?")
        if report is None:
            skills = last if last != "-" else "(no exam result yet)"
            counts = "-"
            tasks = "-"
        else:
            k = settings.bourse_exam_k
            skills = ", ".join(f"{s} {p}/{k}" for s, p in sorted(report.per_skill.items()))
            counts = _exam_count_line(report)
            if report.inconclusive:
                echo(f"  INCONCLUSIVE {aid}: {counts}")
            parts: list[str] = []
            for t in report.tasks:
                if t.outcome == "passed":
                    parts.append(f"{t.skill}:{t.outcome}")
                elif t.outcome == "grader-failed":
                    parts.append(f"{t.skill}:grader-failed[{t.detail}]")
                    echo(f"  FAIL {aid} {t.task_id} {t.skill}: {t.detail}")
                elif t.outcome in ("INFRA", "EXAM_TIMEOUT"):
                    parts.append(f"{t.skill}:{t.outcome}[{t.detail}]")
                    echo(f"  INFRA {aid} {t.task_id} {t.skill}: {t.detail}")
                else:
                    parts.append(f"{t.skill}:{t.outcome}[{t.detail}]")
                    echo(f"  FAIL {aid} {t.task_id} {t.skill}: {t.outcome} {t.detail}")
            tasks = "; ".join(parts) if parts else "-"
        out_rows.append([aid, model, st, skills, counts, tasks])
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
    base = settings.bourse_registry_url.rstrip("/")
    if timeout_s is None:
        counts = _pending_task_counts(base, settings.bourse_exam_k)
        timeout = exam_wait_s(settings, counts if counts else None)
        echo(
            f"wait-active budget {timeout:.0f}s from {sum(counts)} exam tasks in "
            f"{len(counts)} pending exams (concurrency {settings.ollama_num_parallel}, "
            f"task deadline {max_exam_task_deadline_s(settings):.0f}s)"
        )
    else:
        timeout = float(timeout_s)
    started = time.monotonic()
    progress_seen: dict[str, tuple[str, int, int]] = {}
    last_key: tuple[tuple[str, str], ...] | None = None
    last_beat = time.monotonic()
    exam_peak = 0
    roster_warned = False
    while True:
        sampled = _sample_ollama_resident(settings, echo=echo, warn_roster=not roster_warned)
        if sampled is not None:
            exam_peak = max(exam_peak, sampled)
            roster_warned = True  # one warning pass per wait (repeat only if never sampled)
        try:
            with httpx.Client(timeout=10.0) as client:
                rows = registry_agent_rows(client, base)
                if not rows:
                    echo("no agents registered; run stack register")
                    return 1
                pending = [aid for aid, st, _ in rows if st == "PENDING"]
                failed = exam_failed_pending(client, base, pending)
                failed_ids = {aid for aid, _ in failed}
                still_running = [aid for aid in pending if aid not in failed_ids]
                active = sum(r[1] == "ACTIVE" for r in rows)
                key = tuple((r[0], r[1]) for r in rows)
                elapsed = time.monotonic() - started
                now = time.monotonic()
                if key != last_key:
                    echo(
                        f"[{elapsed:5.0f}s] ACTIVE {active}/{len(rows)} "
                        f"pending_exams={len(still_running)}"
                    )
                    for r in rows:
                        echo(f"  {r[0]} {r[1]}")
                    last_key = key
                    last_beat = now
                elif now - last_beat >= HEARTBEAT_S:
                    echo(
                        f"[{elapsed:5.0f}s] heartbeat ACTIVE {active}/{len(rows)} "
                        f"pending_exams={len(still_running)} exam_failed={len(failed_ids)}"
                    )
                    for line in exam_progress_lines(_exam_progress(client, base)):
                        echo(line)
                    last_beat = now
                for line in _new_progress_lines(
                    _exam_progress(client, base), progress_seen, elapsed
                ):
                    echo(line)
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


def _pending_task_counts(base: str, k: int) -> list[int]:
    try:
        with httpx.Client(timeout=10.0) as client:
            agents = [AgentRecord.model_validate(a) for a in client.get(f"{base}/agents").json()]
    except httpx.HTTPError:
        return []
    return [k * len(a.skills) for a in agents if a.state.value == "PENDING"]


def _exam_progress(client: httpx.Client, base: str) -> list[ExamProgress]:
    try:
        resp = client.get(f"{base}/exams/progress")
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    return [ExamProgress.model_validate(p) for p in resp.json()]


def exam_progress_lines(progress: Sequence[ExamProgress]) -> list[str]:
    """One line per running exam (agent, skill, task n/N, attempt, elapsed) + queued list."""
    lines = [
        f"  exam {p.agent_id} {p.skill} task {p.task_n}/{p.task_total} "
        f"attempt {p.attempt} task_elapsed {p.task_elapsed_s:.0f}s/{p.task_deadline_s:.0f}s "
        f"exam_elapsed {p.exam_elapsed_s:.0f}s ({p.mode})"
        for p in progress
        if p.status == "running"
    ]
    queued = [p.agent_id for p in progress if p.status == "queued"]
    if queued:
        lines.append(f"  exam queued: {', '.join(queued)}")
    return lines


def _new_progress_lines(
    progress: Sequence[ExamProgress],
    seen: dict[str, tuple[str, int, int]],
    elapsed: float,
) -> list[str]:
    """Lines for exams whose (skill, task_n, attempt) changed since the last poll."""
    out: list[str] = []
    for p in progress:
        if p.status != "running":
            continue
        key = (p.skill, p.task_n, p.attempt)
        if seen.get(p.agent_id) == key:
            continue
        seen[p.agent_id] = key
        out.append(
            f"[{elapsed:5.0f}s] exam {p.agent_id} {p.skill} task {p.task_n}/{p.task_total} "
            f"attempt {p.attempt} (exam_elapsed {p.exam_elapsed_s:.0f}s)"
        )
    return out


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
            for aid, _st, _ in rows:
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
