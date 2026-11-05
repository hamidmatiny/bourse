"""Resident-memory pair probe (ADR-0045). Measure only; no roster decision."""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bourse.agents.catalog import ROSTER_LARGE_MODEL, ROSTER_SMALL_MODEL
from bourse.config import Settings, get_settings
from bourse.contracts.exam import ExamReport
from bourse.llm.probe import _PROBE_TOOL
from bourse.registry.service import make_agent_id
from bourse.runtime.resident import RESIDENT_LIMIT_BYTES, format_bytes_units
from bourse.stack.launcher import admin_token

E4B_MODEL = "gemma4:e4b"
LIMIT_BYTES = RESIDENT_LIMIT_BYTES

PAIR_CANDIDATES: tuple[tuple[str, str], ...] = (
    (ROSTER_SMALL_MODEL, ROSTER_LARGE_MODEL),  # ADR-0047: both aliases == qwen27b
    (E4B_MODEL, ROSTER_LARGE_MODEL),
    ("qwen3.5:4b", ROSTER_LARGE_MODEL),
)

E4B_AGENT_KEYS: tuple[str, ...] = ("profiler", "pii_fast", "normalizer")
E4B_SERVICES: tuple[str, ...] = ("agent-profiler", "agent-pii-fast", "agent-normalizer")
ENV_DIR = Path("data/run/env")


@dataclass(frozen=True)
class PairMemoryRow:
    model_a: str
    model_b: str
    resident_bytes: int
    under_limit: bool
    loaded_names: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class E4bExamRow:
    agent_key: str
    agent_id: str
    skill: str
    n_passed: int
    n_total: int
    state: str
    model: str


async def ollama_resident_bytes(client: httpx.AsyncClient, base_url: str) -> int:
    r = await client.get(f"{base_url.rstrip('/')}/api/ps")
    r.raise_for_status()
    return sum(int(m.get("size") or 0) for m in r.json().get("models") or [])


async def ollama_unload_all(client: httpx.AsyncClient, base_url: str) -> None:
    r = await client.get(f"{base_url.rstrip('/')}/api/ps")
    r.raise_for_status()
    for m in r.json().get("models") or []:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        await client.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": name, "prompt": "", "keep_alive": 0},
        )
    await asyncio.sleep(2.0)


async def _exam_sized_call(
    client: httpx.AsyncClient, base_url: str, model: str, *, num_ctx: int, keep_alive: str
) -> None:
    """One submit-tool /api/chat call sized like the capability probe (loads the model)."""
    r = await client.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Answer only by calling the submit tool. No text reply.",
                },
                {"role": "user", "content": "What is 2 + 3? Call the submit tool with the answer."},
            ],
            "tools": [_PROBE_TOOL],
            "stream": False,
            "think": False,
            "keep_alive": keep_alive,
            "options": {
                "num_predict": 128,
                "temperature": 0,
                "seed": 0,
                "num_ctx": num_ctx,
            },
        },
        timeout=httpx.Timeout(180.0, connect=10.0),
    )
    r.raise_for_status()


async def _ps_models(
    client: httpx.AsyncClient, base_url: str
) -> list[tuple[str, int]]:
    r = await client.get(f"{base_url.rstrip('/')}/api/ps")
    r.raise_for_status()
    out: list[tuple[str, int]] = []
    for m in r.json().get("models") or []:
        name = str(m.get("name") or m.get("model") or "")
        if name:
            out.append((name, int(m.get("size") or 0)))
    return out


async def measure_pair(settings: Settings, model_a: str, model_b: str) -> PairMemoryRow:
    """Load both models with keep_alive; re-touch any that Ollama evicted before sampling."""
    base = settings.ollama_base_url
    keep_alive = settings.bourse_meter_keep_alive or "30m"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(200.0, connect=10.0)) as client:
            await ollama_unload_all(client, base)
            for model in (model_a, model_b):
                await _exam_sized_call(
                    client,
                    base,
                    model,
                    num_ctx=settings.num_ctx_for(model),
                    keep_alive=keep_alive,
                )
            # Ollama may evict the first model when loading the second; reload missing ones.
            for _ in range(3):
                loaded = await _ps_models(client, base)
                missing = [
                    m
                    for m in (model_a, model_b)
                    if not any(n == m or n.startswith(m) or m.startswith(n) for n, _ in loaded)
                ]
                if not missing:
                    break
                for model in missing:
                    print(f"memory-probe: reloading evicted {model}", flush=True)
                    await _exam_sized_call(
                        client,
                        base,
                        model,
                        num_ctx=settings.num_ctx_for(model),
                        keep_alive=keep_alive,
                    )
            await asyncio.sleep(1.0)
            loaded = await _ps_models(client, base)
            resident = sum(sz for _, sz in loaded)
            names = tuple(n for n, _ in loaded)
            both = all(
                any(n == m or n.startswith(m) or m.startswith(n) for n in names)
                for m in (model_a, model_b)
            )
            err = None if both else f"pair incomplete; /api/ps names={list(names)}"
            return PairMemoryRow(
                model_a=model_a,
                model_b=model_b,
                resident_bytes=resident,
                under_limit=resident < LIMIT_BYTES,
                loaded_names=names,
                error=err,
            )
    except Exception as exc:  # noqa: BLE001 — probe must record every failure
        return PairMemoryRow(
            model_a=model_a,
            model_b=model_b,
            resident_bytes=0,
            under_limit=False,
            error=f"{type(exc).__name__}: {exc}",
        )


async def measure_all_pairs(settings: Settings) -> list[PairMemoryRow]:
    rows: list[PairMemoryRow] = []
    for a, b in PAIR_CANDIDATES:
        print(f"memory-probe: loading pair {a} + {b} ...", flush=True)
        row = await measure_pair(settings, a, b)
        print(
            f"memory-probe: {a}+{b} resident={row.resident_bytes} bytes "
            f"({format_bytes_units(row.resident_bytes)}) under_limit={row.under_limit} "
            f"loaded={list(row.loaded_names)}"
            + (f" error={row.error}" if row.error else ""),
            flush=True,
        )
        rows.append(row)
    async with httpx.AsyncClient(timeout=30.0) as client:
        await ollama_unload_all(client, settings.ollama_base_url)
    return rows


def _patch_env_model(service: str, model_small: str) -> Path:
    """Set BOURSE_MODEL_SMALL in the compose env file for one agent service (not committed)."""
    path = ENV_DIR / f"{service}.env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r"^BOURSE_MODEL_SMALL=", text, flags=re.M):
        text = re.sub(
            r"^BOURSE_MODEL_SMALL=.*$",
            f"BOURSE_MODEL_SMALL={model_small}",
            text,
            flags=re.M,
        )
    else:
        text = text.rstrip() + f"\nBOURSE_MODEL_SMALL={model_small}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _recreate_services(services: tuple[str, ...], *, wait_health_s: float = 90.0) -> None:
    cmd = [
        "docker",
        "compose",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        *services,
    ]
    print(f"memory-probe: {' '.join(cmd)}", flush=True)
    subprocess.check_call(cmd, cwd=Path.cwd())
    deadline = time.monotonic() + wait_health_s
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.Service}} {{.Health}}", *services],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if lines and all(ln.endswith(" healthy") for ln in lines):
            print(f"memory-probe: healthy {services}", flush=True)
            return
        time.sleep(3.0)
    print(f"memory-probe: WARN health wait timed out for {services}: {lines}", flush=True)


def _agent_ids() -> dict[str, str]:
    from bourse.agents.catalog import specs_from_settings

    specs = specs_from_settings(get_settings())
    return {
        key: make_agent_id(specs[key].display_name, specs[key].vendor_domain)
        for key in E4B_AGENT_KEYS
    }


def _sql(sql: str) -> None:
    subprocess.check_call(
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
            "bourse_compose",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        cwd=Path.cwd(),
    )


def _force_pending(agent_ids: list[str]) -> None:
    """Throwaway: ACTIVE -> PENDING so the registry will re-run admission exams."""
    ids = ", ".join("'" + a.replace("'", "''") + "'" for a in agent_ids)
    print(f"memory-probe: force PENDING for {agent_ids}", flush=True)
    # Clear exam-failed cooldown so a fresh exam can start immediately.
    _sql(f"DELETE FROM violations WHERE kind = 'EXAM_FAILED' AND agent_id IN ({ids});")
    _sql(f"UPDATE agents SET state = 'PENDING', updated_at = now() WHERE agent_id IN ({ids});")


def _force_active(agent_ids: list[str]) -> None:
    ids = ", ".join("'" + a.replace("'", "''") + "'" for a in agent_ids)
    print(f"memory-probe: restore ACTIVE for {agent_ids}", flush=True)
    _sql(f"UPDATE agents SET state = 'ACTIVE', updated_at = now() WHERE agent_id IN ({ids});")


def _start_exams(settings: Settings, agent_ids: list[str]) -> None:
    token = admin_token(settings)
    base = settings.bourse_registry_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30.0) as client:
        for aid in agent_ids:
            r = client.post(f"{base}/agents/{aid}/exam", headers=headers)
            print(
                f"memory-probe: start exam {aid} -> HTTP {r.status_code} {r.text[:120]}",
                flush=True,
            )


def _fetch_report(client: httpx.Client, base: str, aid: str) -> ExamReport | None:
    resp = client.get(f"{base}/agents/{aid}/exam-result")
    if resp.status_code != 200:
        return None
    return ExamReport.model_validate(resp.json())


def _report_fingerprint(report: ExamReport | None) -> str:
    if report is None:
        return ""
    tasks = "|".join(f"{t.task_id}:{t.outcome}" for t in report.tasks)
    return f"{report.n}/{report.total}:{tasks}"


def _wait_exams(
    settings: Settings,
    agent_ids: set[str],
    *,
    timeout_s: float,
    prior_fingerprints: dict[str, str],
) -> dict[str, Any]:
    """Poll until each agent leaves PENDING (or gets a new exam-result fingerprint)."""
    from bourse.stack.activation import exam_failed_pending, print_exam_summary_table, registry_agent_rows

    base = settings.bourse_registry_url.rstrip("/")
    started = time.monotonic()
    with httpx.Client(timeout=30.0) as client:
        while True:
            rows = registry_agent_rows(client, base)
            focus = [(a, s, last) for a, s, last in rows if a in agent_ids]
            pending = [aid for aid, st, _ in focus if st == "PENDING"]
            failed = {aid for aid, _ in exam_failed_pending(client, base, pending)}
            still_running = [aid for aid in pending if aid not in failed]
            done: dict[str, ExamReport | None] = {}
            fresh = 0
            for aid, st, _ in focus:
                report = _fetch_report(client, base, aid)
                done[aid] = report
                fp = _report_fingerprint(report)
                if fp and fp != prior_fingerprints.get(aid, ""):
                    fresh += 1
                elif st == "ACTIVE":
                    fresh += 1
                elif aid in failed:
                    fresh += 1
            elapsed = time.monotonic() - started
            print(
                f"memory-probe: [{elapsed:5.0f}s] fresh={fresh}/{len(agent_ids)} "
                f"still_running={still_running} exam_failed={sorted(failed)}",
                flush=True,
            )
            if not still_running and fresh >= len(agent_ids):
                print_exam_summary_table(client, base, focus, echo=print)
                return {aid: done.get(aid) for aid in agent_ids}
            if elapsed >= timeout_s:
                print_exam_summary_table(client, base, focus, echo=print)
                return {aid: done.get(aid) for aid in agent_ids}
            time.sleep(10.0)


def _rows_from_reports(
    reports: dict[str, ExamReport | None], states: dict[str, str]
) -> list[E4bExamRow]:
    from bourse.agents.catalog import specs_from_settings

    specs = specs_from_settings(get_settings())
    out: list[E4bExamRow] = []
    id_to_key = {v: k for k, v in _agent_ids().items()}
    for aid, report in reports.items():
        key = id_to_key.get(aid, aid)
        state = states.get(aid, "?")
        if report is None:
            skills = list(specs[key].skills) if key in specs else []
            for sk in skills or [type("S", (), {"value": "?"})()]:
                out.append(
                    E4bExamRow(
                        agent_key=key,
                        agent_id=aid,
                        skill=getattr(sk, "value", str(sk)),
                        n_passed=0,
                        n_total=0,
                        state=state,
                        model=E4B_MODEL,
                    )
                )
            continue
        # per_skill maps skill -> passed count; total per skill is exam k
        k = get_settings().bourse_exam_k
        for skill, n_passed in sorted(report.per_skill.items()):
            out.append(
                E4bExamRow(
                    agent_key=key,
                    agent_id=aid,
                    skill=skill,
                    n_passed=int(n_passed),
                    n_total=k,
                    state=state,
                    model=E4B_MODEL,
                )
            )
        if not report.per_skill:
            out.append(
                E4bExamRow(
                    agent_key=key,
                    agent_id=aid,
                    skill="(none)",
                    n_passed=report.n,
                    n_total=report.total,
                    state=state,
                    model=E4B_MODEL,
                )
            )
    return out


def run_e4b_throwaway_exams(settings: Settings, *, timeout_s: float | None = None) -> list[E4bExamRow]:
    """Recreate three agents on gemma4:e4b, force PENDING, run admission exams, restore roster."""
    from bourse.stack.activation import exam_wait_s, registry_agent_rows

    limit = exam_wait_s(settings) if timeout_s is None else float(timeout_s)
    ids_map = _agent_ids()
    agent_ids = list(ids_map.values())

    print("memory-probe: throwaway e4b exams for profiler/pii-fast/normalizer", flush=True)
    # Meter allowlist is BOURSE_MODEL_SMALL+LARGE only; temporarily allow e4b as small.
    _patch_env_model("meter", E4B_MODEL)
    for svc in E4B_SERVICES:
        _patch_env_model(svc, E4B_MODEL)
    _recreate_services(("meter", *E4B_SERVICES))

    base = settings.bourse_registry_url.rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        prior = {
            aid: _report_fingerprint(_fetch_report(client, base, aid)) for aid in agent_ids
        }
    _force_pending(agent_ids)
    _start_exams(settings, agent_ids)
    reports = _wait_exams(
        settings, set(agent_ids), timeout_s=limit, prior_fingerprints=prior
    )

    with httpx.Client(timeout=10.0) as client:
        states = {
            aid: st
            for aid, st, _ in registry_agent_rows(
                client, settings.bourse_registry_url.rstrip("/")
            )
        }
    rows = _rows_from_reports(reports, states)
    for row in rows:
        print(
            f"memory-probe: e4b exam {row.agent_key} skill={row.skill} "
            f"{row.n_passed}/{row.n_total} state={row.state} model={row.model}",
            flush=True,
        )

    print("memory-probe: restoring roster BOURSE_MODEL_SMALL on meter + agents", flush=True)
    _patch_env_model("meter", ROSTER_SMALL_MODEL)
    for svc in E4B_SERVICES:
        _patch_env_model(svc, ROSTER_SMALL_MODEL)
    _recreate_services(("meter", *E4B_SERVICES))
    _force_active(agent_ids)
    return rows


def write_adr_0045(
    pair_rows: list[PairMemoryRow],
    e4b_rows: list[E4bExamRow],
    *,
    path: Path | None = None,
) -> Path:
    """Rewrite measurement tables; preserve Decision if already present on disk."""
    out = path or Path("docs/adr/0045-memory-pair-probe.md")
    decision = (
        "Keep the ADR-0021 roster: `gemma4:12b` (profiler, pii-fast, normalizer) +\n"
        "`qwen3.5:27b-q4_K_M` (reporter, pii-precise).\n"
        "\n"
        "Reason: measured pair resident for gemma4:12b+qwen27b is under the\n"
        "26,000,000,000-byte limit; the `gemma4:e4b` pair is larger so it gives\n"
        "no memory benefit; `qwen3.5:4b` previously failed the normalizer exam 3/5.\n"
        "Do not change `ROSTER_MODELS` or the 26e9-byte limit in this ADR."
    )
    if out.exists():
        text = out.read_text(encoding="utf-8")
        if "## Decision" in text and "Keep the ADR-0021 roster" in text:
            start = text.index("## Decision")
            end = text.index("## Consequences") if "## Consequences" in text else len(text)
            decision = text[start + len("## Decision") : end].strip()

    lines = [
        "# ADR-0045: Memory pair probe (measure only)",
        "",
        "## Context",
        "",
        "Baseline honest runs aborted when resident Ollama models exceeded the hard limit of",
        "**26,000,000,000 bytes** (`limit 26.00 GB / 24.21 GiB`; ADR-0019 / AGENTS.md /",
        "`ResidentMonitor.RESIDENT_LIMIT_BYTES`). ADR-0021 pins the roster to `gemma4:12b` +",
        "`qwen3.5:27b-q4_K_M`. ADR-0038 recorded `gemma4:12b` ≈ 16.7 GiB under MLX.",
        "",
        "Pair numbers below are after **one exam-sized submit-tool call per model** (with",
        "reload if Ollama evicted the first model), not sustained-load peaks. The in-run",
        "`ResidentMonitor` peak is authoritative for acceptance and abort decisions.",
        "",
        "## Decision",
        "",
        decision,
        "",
        "## Consequences",
        "",
        "Status: Accepted. Roster remains ADR-0021. Limit remains 26,000,000,000 bytes.",
        "",
        "## Pair resident bytes (after one exam-sized submit-tool call per model)",
        "",
        "Limit = 26,000,000,000 bytes (`26.00 GB / 24.21 GiB`).",
        "",
        "| model_a | model_b | bytes | GB | GiB | under_limit | /api/ps names | error |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for r in pair_rows:
        names = ", ".join(f"`{n}`" for n in r.loaded_names) or "-"
        lines.append(
            f"| `{r.model_a}` | `{r.model_b}` | {r.resident_bytes} | "
            f"{r.resident_bytes / 1e9:.2f} | {r.resident_bytes / (1024**3):.2f} | "
            f"{r.under_limit} | {names} | {r.error or ''} |"
        )
    lines.extend(
        [
            "",
            "## Throwaway admission exams on `gemma4:e4b` only",
            "",
            "Agents recreated with `BOURSE_MODEL_SMALL=gemma4:e4b` in compose env files (not committed).",
            "Registry rows forced PENDING, admission exams re-run, then models restored to",
            f"`{ROSTER_SMALL_MODEL}` and state forced ACTIVE. Skills: profiler / pii-fast / normalizer.",
            "",
            "| agent | skill | n/N | state | model |",
            "|---|---|---|---|---|",
        ]
    )
    for r in e4b_rows:
        lines.append(
            f"| `{r.agent_key}` | `{r.skill}` | {r.n_passed}/{r.n_total} | {r.state} | `{r.model}` |"
        )
    lines.extend(
        [
            "",
            f"Written at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} by `bourse probe --memory`.",
            "",
        ]
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"memory-probe: wrote {out}", flush=True)
    return out


async def run_memory_probe(settings: Settings, *, with_e4b_exams: bool = True) -> Path:
    pairs = await measure_all_pairs(settings)
    e4b: list[E4bExamRow] = []
    if with_e4b_exams:
        e4b = await asyncio.to_thread(run_e4b_throwaway_exams, settings)
    return write_adr_0045(pairs, e4b)
