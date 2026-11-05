#!/usr/bin/env python3
"""Throwaway resident candidate measurements for ADR-0046 (no committed roster change).

OLLAMA_NUM_PARALLEL is a HOST Ollama setting. This script only aligns the meter gate
(config / compose env) to match; it never starts/stops/restarts host Ollama.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bourse.agents.catalog import ROSTER_LARGE_MODEL, ROSTER_SMALL_MODEL, specs_from_settings
from bourse.config import get_settings
from bourse.contracts.exam import ExamReport
from bourse.registry.service import make_agent_id
from bourse.runtime.resident import RESIDENT_LIMIT_BYTES, format_bytes_units
from bourse.stack.activation import (
    exam_failed_pending,
    print_exam_summary_table,
    registry_agent_rows,
)
from bourse.stack.launcher import admin_token

ENV_DIR = Path("data/run/env")
OUT = Path("data/probe/adr-0046-candidates.json")
METRICS_DIR = Path("data/metrics")
ALL_AGENT_KEYS = ("profiler", "pii_precise", "pii_fast", "normalizer", "reporter")
SMALL_AGENT_KEYS = ("profiler", "pii_fast", "normalizer")
ALL_SERVICES = tuple(f"agent-{k.replace('_', '-')}" for k in ALL_AGENT_KEYS)


class MeasureFailed(SystemExit):
    """Clean process exit with one stderr line (no traceback)."""

    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(code)
        print(f"MEASURE FAILED: {msg}", file=sys.stderr, flush=True)


@dataclass
class SkillScore:
    agent: str
    skill: str
    n_passed: int
    n_total: int


@dataclass
class ModelPeak:
    name: str
    min_size: int
    max_size: int
    last_size: int
    runner: str = ""
    context_length: int | None = None


@dataclass
class CandidateResult:
    candidate_id: str
    description: str
    meter_parallel: int
    small_model: str
    exam_peak_bytes: int = 0
    baseline_peak_bytes: int = 0
    combined_peak_bytes: int = 0
    exam_wall_s: float = 0.0
    baseline_wall_s: float = 0.0
    baseline_ok: int = 0
    baseline_n: int = 0
    baseline_statuses: list[str] = field(default_factory=list)
    abort_reasons: list[str] = field(default_factory=list)
    exam_scores: list[SkillScore] = field(default_factory=list)
    model_peaks: list[ModelPeak] = field(default_factory=list)
    metrics_file: str = ""
    notes: str = ""


def _patch_env(service: str, key: str, value: str) -> None:
    path = ENV_DIR / f"{service}.env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip() + f"\n{key}={value}\n"
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _recreate(*services: str, wait_s: float = 180.0) -> None:
    cmd = ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", *services]
    print("recreate:", " ".join(cmd), flush=True)
    try:
        subprocess.check_call(cmd, cwd=Path.cwd())
    except subprocess.CalledProcessError as exc:
        raise MeasureFailed(f"docker compose recreate failed rc={exc.returncode}") from None
    deadline = time.monotonic() + wait_s
    last = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.Service}} {{.Health}}", *services],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        last = " | ".join(lines)
        if lines and all("healthy" in ln for ln in lines):
            print("healthy:", services, flush=True)
            return
        time.sleep(3.0)
    raise MeasureFailed(f"health wait timed out for {services}: {last}")


def _sql(sql: str) -> None:
    try:
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
    except subprocess.CalledProcessError as exc:
        raise MeasureFailed(f"psql failed rc={exc.returncode}") from None


def _agent_ids(keys: tuple[str, ...] = ALL_AGENT_KEYS) -> dict[str, str]:
    specs = specs_from_settings(get_settings())
    return {k: make_agent_id(specs[k].display_name, specs[k].vendor_domain) for k in keys}


def _force_pending(ids: list[str]) -> None:
    quoted = ", ".join("'" + a.replace("'", "''") + "'" for a in ids)
    _sql(f"DELETE FROM violations WHERE kind = 'EXAM_FAILED' AND agent_id IN ({quoted});")
    _sql(f"UPDATE agents SET state = 'PENDING', updated_at = now() WHERE agent_id IN ({quoted});")


def _force_active(ids: list[str]) -> None:
    quoted = ", ".join("'" + a.replace("'", "''") + "'" for a in ids)
    _sql(f"UPDATE agents SET state = 'ACTIVE', updated_at = now() WHERE agent_id IN ({quoted});")


def _fetch_report(client: httpx.Client, base: str, aid: str) -> ExamReport | None:
    r = client.get(f"{base}/agents/{aid}/exam-result")
    if r.status_code != 200:
        return None
    return ExamReport.model_validate(r.json())


def _fp(report: ExamReport | None) -> str:
    if report is None:
        return ""
    return "|".join(f"{t.task_id}:{t.outcome}" for t in report.tasks)


def _analyze_metrics(path: Path) -> tuple[int, list[ModelPeak]]:
    if not path.exists():
        return 0, []
    peak = 0
    per: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"min": None, "max": 0, "last": 0, "runner": "", "ctx": None}
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        peak = max(peak, int(row.get("resident_bytes") or 0))
        for m in row.get("models") or []:
            name = str(m.get("name") or "")
            size = int(m.get("size") or 0)
            slot = per[name]
            slot["min"] = size if slot["min"] is None else min(slot["min"], size)
            slot["max"] = max(slot["max"], size)
            slot["last"] = size
            slot["runner"] = str(m.get("runner") or slot["runner"] or "")
            ctx = m.get("context_length")
            if ctx is not None:
                slot["ctx"] = int(ctx)
    models = [
        ModelPeak(
            name=n,
            min_size=int(v["min"] or 0),
            max_size=int(v["max"]),
            last_size=int(v["last"]),
            runner=str(v["runner"]),
            context_length=v["ctx"],
        )
        for n, v in sorted(per.items())
    ]
    return peak, models


def _ensure_registry() -> None:
    settings = get_settings()
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{settings.bourse_registry_url.rstrip('/')}/agents")
            if r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) >= 5:
                print(f"registry ok agents={len(r.json())}", flush=True)
                return
    except httpx.HTTPError:
        pass
    print(
        "registry/agents not ready; stack up --with-adversaries "
        "(host Ollama parallel already set by operator; meter gate via env)",
        flush=True,
    )
    # write_compose_env reads settings.ollama_num_parallel — pass env so gate=1 is embedded.
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "OLLAMA_NUM_PARALLEL": "1",
    }
    try:
        subprocess.check_call(
            [
                "uv",
                "run",
                "bourse",
                "stack",
                "up",
                "--mode",
                "compose",
                "--with-adversaries",
            ],
            cwd=Path.cwd(),
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise MeasureFailed(f"stack up --with-adversaries failed rc={exc.returncode}") from None


def _meter_parallel_live() -> int | None:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "meter", "printenv", "OLLAMA_NUM_PARALLEL"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def set_meter_parallel(n: int) -> None:
    """Align meter/agent compose env gate only. Does not touch host Ollama."""
    print(
        f"meter gate OLLAMA_NUM_PARALLEL={n} (host Ollama must already match; not touched)",
        flush=True,
    )
    _patch_env("meter", "OLLAMA_NUM_PARALLEL", str(n))
    for svc in ALL_SERVICES:
        _patch_env(svc, "OLLAMA_NUM_PARALLEL", str(n))
    live = _meter_parallel_live()
    if live == n:
        print(f"meter already live OLLAMA_NUM_PARALLEL={live}; skip recreate", flush=True)
        return
    # Recreate meter first, then agents: a single `up` of meter+agents can hang on
    # "meter Waiting" under low Docker VM memory even with --no-deps.
    _recreate("meter")
    _recreate(*ALL_SERVICES)


def set_small_model(model: str, keys: tuple[str, ...] = SMALL_AGENT_KEYS) -> None:
    """Throwaway BOURSE_MODEL_SMALL on selected agents (+ meter allowlist)."""
    print(f"throwaway BOURSE_MODEL_SMALL={model} on {keys}", flush=True)
    _patch_env("meter", "BOURSE_MODEL_SMALL", model)
    for key in keys:
        svc = f"agent-{key.replace('_', '-')}"
        _patch_env(svc, "BOURSE_MODEL_SMALL", model)
    agent_svcs = tuple(f"agent-{k.replace('_', '-')}" for k in keys)
    _recreate("meter")
    _recreate(*agent_svcs)


def restore_roster_env(*, parallel: int = 2) -> None:
    """Restore ADR-0021 roster models + meter gate. Default parallel=2 (test-only was 1)."""
    print(
        f"restoring meter/agents env to roster + parallel={parallel} "
        f"(host Ollama not touched; operator must match)",
        flush=True,
    )
    _patch_env("meter", "BOURSE_MODEL_SMALL", ROSTER_SMALL_MODEL)
    _patch_env("meter", "BOURSE_MODEL_LARGE", ROSTER_LARGE_MODEL)
    _patch_env("meter", "OLLAMA_NUM_PARALLEL", str(parallel))
    for key in ALL_AGENT_KEYS:
        svc = f"agent-{key.replace('_', '-')}"
        # roster: small agents use small model, large use large — env only has SMALL/LARGE knobs
        _patch_env(svc, "BOURSE_MODEL_SMALL", ROSTER_SMALL_MODEL)
        _patch_env(svc, "BOURSE_MODEL_LARGE", ROSTER_LARGE_MODEL)
        _patch_env(svc, "OLLAMA_NUM_PARALLEL", str(parallel))
        # clear throwaway card version if present
        path = ENV_DIR / f"{svc}.env"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if re.search(r"^BOURSE_CARD_VERSION=", text, flags=re.M):
                text = re.sub(r"^BOURSE_CARD_VERSION=.*\n?", "", text, flags=re.M)
                path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    _recreate("meter")
    _recreate(*ALL_SERVICES)
    _force_active(list(_agent_ids().values()))


def unload_model(model: str) -> None:
    """Ask host Ollama to drop a resident (keep_alive=0). Does not change NUM_PARALLEL."""
    print(f"unload {model} keep_alive=0", flush=True)
    try:
        with httpx.Client(timeout=120.0) as client:
            client.post(
                f"{get_settings().ollama_base_url.rstrip('/')}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": 0,
                    "stream": False,
                    "think": False,
                },
            )
    except httpx.HTTPError as exc:
        raise MeasureFailed(f"unload {model} failed: {exc}") from None


def verify_ps_models(
    *, allowed: set[str] | None = None, forbidden: set[str] | None = None
) -> list[str]:
    """Return live /api/ps model names; raise MeasureFailed if constraints violated."""
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{get_settings().ollama_base_url.rstrip('/')}/api/ps")
            r.raise_for_status()
            names = [str(m.get("name") or "") for m in (r.json().get("models") or [])]
    except httpx.HTTPError as exc:
        raise MeasureFailed(f"/api/ps failed: {exc}") from None
    if forbidden:
        bad = [n for n in names if n in forbidden or any(n.startswith(f) for f in forbidden)]
        if bad:
            raise MeasureFailed(f"/api/ps still has forbidden models: {bad} (live={names})")
    if allowed is not None:
        extra = [n for n in names if n not in allowed]
        if extra:
            raise MeasureFailed(f"/api/ps unexpected models: {extra} (allowed={sorted(allowed)})")
    print(f"/api/ps models={names}", flush=True)
    return names


def set_all_agents_model(model: str, *, card_version: str) -> None:
    """Throwaway: BOURSE_MODEL_SMALL=LARGE=model on meter + all 5 honest agents."""
    print(f"throwaway all agents model={model} card_version={card_version}", flush=True)
    _patch_env("meter", "BOURSE_MODEL_SMALL", model)
    _patch_env("meter", "BOURSE_MODEL_LARGE", model)
    for key in ALL_AGENT_KEYS:
        svc = f"agent-{key.replace('_', '-')}"
        _patch_env(svc, "BOURSE_MODEL_SMALL", model)
        _patch_env(svc, "BOURSE_MODEL_LARGE", model)
        _patch_env(svc, "BOURSE_CARD_VERSION", card_version)
    _recreate("meter")
    _recreate(*ALL_SERVICES)


def reregister() -> None:
    print("re-register agents", flush=True)
    try:
        subprocess.check_call(
            ["uv", "run", "bourse", "stack", "register"],
            cwd=Path.cwd(),
        )
    except subprocess.CalledProcessError as exc:
        raise MeasureFailed(f"stack register failed rc={exc.returncode}") from None


def patch_all_env_parallel(n: int) -> None:
    """Write OLLAMA_NUM_PARALLEL=n into every data/run/env/*.env (meter gate files)."""
    for path in sorted(ENV_DIR.glob("*.env")):
        _patch_env(path.stem, "OLLAMA_NUM_PARALLEL", str(n))
    print(f"patched OLLAMA_NUM_PARALLEL={n} in {ENV_DIR}/*.env", flush=True)


def run_exams(
    agent_keys: tuple[str, ...] = ALL_AGENT_KEYS,
) -> tuple[float, dict[str, ExamReport | None], list[SkillScore]]:
    settings = get_settings()
    ids_map = _agent_ids(agent_keys)
    agent_ids = list(ids_map.values())
    base = settings.bourse_registry_url.rstrip("/")
    k = settings.bourse_exam_k

    with httpx.Client(timeout=30.0) as client:
        prior = {aid: _fp(_fetch_report(client, base, aid)) for aid in agent_ids}

    _force_pending(agent_ids)
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    with httpx.Client(timeout=30.0) as client:
        for aid in agent_ids:
            r = client.post(f"{base}/agents/{aid}/exam", headers=headers)
            print(f"exam start {aid} -> HTTP {r.status_code}", flush=True)
            if r.status_code not in (200, 202):
                raise MeasureFailed(f"start exam {aid}: HTTP {r.status_code} {r.text[:200]}")

    started = time.monotonic()
    reports: dict[str, ExamReport | None] = {}
    with httpx.Client(timeout=30.0) as client:
        while True:
            rows = registry_agent_rows(client, base)
            focus = [(a, s, last) for a, s, last in rows if a in set(agent_ids)]
            pending = [aid for aid, st, _ in focus if st == "PENDING"]
            failed = {aid for aid, _ in exam_failed_pending(client, base, pending)}
            still = [aid for aid in pending if aid not in failed]
            fresh = 0
            for aid, st, _ in focus:
                rep = _fetch_report(client, base, aid)
                reports[aid] = rep
                if (_fp(rep) and _fp(rep) != prior.get(aid, "")) or st == "ACTIVE" or aid in failed:
                    fresh += 1
            elapsed = time.monotonic() - started
            print(
                f"[{elapsed:5.0f}s] exam fresh={fresh}/{len(agent_ids)} still={still}",
                flush=True,
            )
            if not still and fresh >= len(agent_ids):
                print_exam_summary_table(client, base, focus, echo=print)
                break
            if elapsed > float(settings.bourse_exam_timeout_s) + 120.0:
                print_exam_summary_table(client, base, focus, echo=print)
                raise MeasureFailed(f"exam wait timed out after {elapsed:.0f}s still={still}")
            time.sleep(10.0)
    wall = time.monotonic() - started
    scores: list[SkillScore] = []
    id_to_key = {v: k for k, v in ids_map.items()}
    for aid, rep in reports.items():
        key = id_to_key.get(aid, aid)
        if rep is None:
            continue
        for skill, n in sorted(rep.per_skill.items()):
            scores.append(SkillScore(agent=key, skill=skill, n_passed=int(n), n_total=k))
    return wall, reports, scores


def run_baselines(n: int = 3) -> tuple[float, int, list[str], list[str], Path, int]:
    """n broker runs inside compose (agents/meter are not on host ports).

    Host defaults hit DB `bourse` (no `runs` table) and cannot resolve
    `*.vendor-*.local` agent URLs. Use the tools-profile broker container + broker.env.
    """
    script = Path("data/probe/_baseline3_in_compose.py")
    if not script.is_file():
        raise MeasureFailed(f"missing {script}")
    summary = Path("data/probe/adr-0046-baseline-last.json")
    if summary.exists():
        summary.unlink()
    cmd = [
        "docker",
        "compose",
        "--profile",
        "tools",
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "broker",
        f"/app/{script.as_posix()}",
        "--runs",
        str(n),
        "--summary",
        f"/app/{summary.as_posix()}",
    ]
    print("baseline via compose:", " ".join(cmd), flush=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=Path.cwd(), check=False)
    except OSError as exc:
        raise MeasureFailed(f"compose baseline spawn failed: {exc}") from None
    wall = time.monotonic() - t0
    if not summary.is_file():
        raise MeasureFailed(
            f"compose baseline produced no summary (rc={proc.returncode}); see container logs"
        )
    data = json.loads(summary.read_text(encoding="utf-8"))
    metrics = Path(str(data.get("metrics_file") or ""))
    peak = int(data.get("peak") or 0)
    ok = int(data.get("baseline_ok") or 0)
    statuses = [str(s) for s in (data.get("statuses") or [])]
    aborts = [str(a) for a in (data.get("aborts") or [])]
    # prefer in-container wall if present
    wall = float(data.get("wall_s") or wall)
    if proc.returncode not in (0, 1):
        raise MeasureFailed(f"compose baseline rc={proc.returncode}")
    if not metrics.is_file():
        # metrics path may be container-relative under /app/data → host data/
        alt = Path(str(metrics).removeprefix("/app/"))
        if alt.is_file():
            metrics = alt
    return wall, ok, statuses, aborts, metrics, peak


def _sample_ps_peak(settings, peak: int) -> int:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{settings.ollama_base_url.rstrip('/')}/api/ps")
            r.raise_for_status()
            total = sum(int(m.get("size") or 0) for m in r.json().get("models") or [])
            return max(peak, total)
    except Exception:
        return peak


def run_exams_with_peak(
    agent_keys: tuple[str, ...] = ALL_AGENT_KEYS,
) -> tuple[float, int, list[SkillScore]]:
    settings = get_settings()
    ids_map = _agent_ids(agent_keys)
    agent_ids = list(ids_map.values())
    base = settings.bourse_registry_url.rstrip("/")
    k = settings.bourse_exam_k

    with httpx.Client(timeout=30.0) as client:
        prior = {aid: _fp(_fetch_report(client, base, aid)) for aid in agent_ids}

    _force_pending(agent_ids)
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    with httpx.Client(timeout=30.0) as client:
        for aid in agent_ids:
            r = client.post(f"{base}/agents/{aid}/exam", headers=headers)
            print(f"exam start {aid} -> HTTP {r.status_code}", flush=True)
            if r.status_code not in (200, 202):
                raise MeasureFailed(f"start exam {aid}: HTTP {r.status_code} {r.text[:200]}")

    started = time.monotonic()
    exam_peak = 0
    reports: dict[str, ExamReport | None] = {}
    with httpx.Client(timeout=30.0) as client:
        while True:
            exam_peak = _sample_ps_peak(settings, exam_peak)
            rows = registry_agent_rows(client, base)
            focus = [(a, s, last) for a, s, last in rows if a in set(agent_ids)]
            pending = [aid for aid, st, _ in focus if st == "PENDING"]
            failed = {aid for aid, _ in exam_failed_pending(client, base, pending)}
            still = [aid for aid in pending if aid not in failed]
            fresh = 0
            for aid, st, _ in focus:
                rep = _fetch_report(client, base, aid)
                reports[aid] = rep
                if (_fp(rep) and _fp(rep) != prior.get(aid, "")) or st == "ACTIVE" or aid in failed:
                    fresh += 1
            elapsed = time.monotonic() - started
            print(
                f"[{elapsed:5.0f}s] exam fresh={fresh}/{len(agent_ids)} still={still} "
                f"ps_peak={exam_peak} ({format_bytes_units(exam_peak)})",
                flush=True,
            )
            if not still and fresh >= len(agent_ids):
                print_exam_summary_table(client, base, focus, echo=print)
                break
            if elapsed > float(settings.bourse_exam_timeout_s) + 120.0:
                print_exam_summary_table(client, base, focus, echo=print)
                raise MeasureFailed(f"exam wait timed out after {elapsed:.0f}s still={still}")
            time.sleep(5.0)
    wall = time.monotonic() - started
    scores: list[SkillScore] = []
    id_to_key = {v: k for k, v in ids_map.items()}
    for aid, rep in reports.items():
        key = id_to_key.get(aid, aid)
        if rep is None:
            continue
        for skill, n in sorted(rep.per_skill.items()):
            scores.append(SkillScore(agent=key, skill=skill, n_passed=int(n), n_total=k))
    return wall, exam_peak, scores


def measure(
    *,
    candidate_id: str,
    description: str,
    meter_parallel: int,
    small_model: str,
    baseline_runs: int = 3,
) -> CandidateResult:
    set_meter_parallel(meter_parallel)
    if small_model != ROSTER_SMALL_MODEL:
        set_small_model(small_model, SMALL_AGENT_KEYS)

    exam_wall, exam_peak, scores = run_exams_with_peak(ALL_AGENT_KEYS)
    base_wall, ok, statuses, aborts, metrics, base_peak = run_baselines(baseline_runs)
    file_peak, model_peaks = _analyze_metrics(metrics)
    combined = max(exam_peak, base_peak, file_peak)

    return CandidateResult(
        candidate_id=candidate_id,
        description=description,
        meter_parallel=meter_parallel,
        small_model=small_model,
        exam_peak_bytes=exam_peak,
        baseline_peak_bytes=max(base_peak, file_peak),
        combined_peak_bytes=combined,
        exam_wall_s=round(exam_wall, 1),
        baseline_wall_s=round(base_wall, 1),
        baseline_ok=ok,
        baseline_n=baseline_runs,
        baseline_statuses=statuses,
        abort_reasons=aborts,
        exam_scores=scores,
        model_peaks=model_peaks,
        metrics_file=str(metrics),
        notes=(
            f"host OLLAMA_NUM_PARALLEL assumed={meter_parallel}; "
            f"meter gate set to {meter_parallel}; limit={RESIDENT_LIMIT_BYTES}"
        ),
    )


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    results: list[CandidateResult] = []
    try:
        _ensure_registry()
        print("=== candidate b: mlx gemma4:12b + qwen27b, meter/host parallel=1 ===", flush=True)
        b = measure(
            candidate_id="b-mlx-gemma12b-parallel1",
            description=(
                "current gemma4:12b (mlx) + qwen27b; host OLLAMA_NUM_PARALLEL=1; "
                "meter gate=1; exams + 3-run baseline"
            ),
            meter_parallel=1,
            small_model=ROSTER_SMALL_MODEL,
            baseline_runs=3,
        )
        results.append(b)
        print(
            f"b combined_peak={b.combined_peak_bytes} "
            f"({format_bytes_units(b.combined_peak_bytes)}) "
            f"baseline={b.baseline_ok}/{b.baseline_n} exam_wall={b.exam_wall_s}s "
            f"baseline_wall={b.baseline_wall_s}s",
            flush=True,
        )
        for mp in b.model_peaks:
            print(
                f"  model {mp.name} min={mp.min_size} max={mp.max_size} last={mp.last_size} "
                f"runner={mp.runner} ctx={mp.context_length}",
                flush=True,
            )

        if b.combined_peak_bytes > RESIDENT_LIMIT_BYTES:
            print(
                "=== candidate c: qwen3.5:4b small agents + qwen27b large, parallel=1 ===",
                flush=True,
            )
            c = measure(
                candidate_id="c-qwen4b-small-parallel1",
                description=(
                    "qwen3.5:4b for profiler/pii-fast/normalizer; large agents keep qwen27b; "
                    "host/meter parallel=1; exams + 3-run baseline"
                ),
                meter_parallel=1,
                small_model="qwen3.5:4b",
                baseline_runs=3,
            )
            results.append(c)
            print(
                f"c combined_peak={c.combined_peak_bytes} "
                f"({format_bytes_units(c.combined_peak_bytes)}) "
                f"baseline={c.baseline_ok}/{c.baseline_n}",
                flush=True,
            )
    except MeasureFailed:
        restore_roster_env()
        raise
    except Exception as exc:
        restore_roster_env()
        raise MeasureFailed(f"{type(exc).__name__}: {exc}") from None
    finally:
        try:
            restore_roster_env()
        except Exception as exc:
            print(f"restore warning: {exc}", file=sys.stderr, flush=True)

    payload = {
        "limit_bytes": RESIDENT_LIMIT_BYTES,
        "limit_human": format_bytes_units(RESIDENT_LIMIT_BYTES),
        "candidates": [
            {
                **{k: v for k, v in asdict(c).items() if k not in {"exam_scores", "model_peaks"}},
                "exam_scores": [asdict(s) for s in c.exam_scores],
                "model_peaks": [asdict(m) for m in c.model_peaks],
            }
            for c in results
        ],
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    try:
        main()
    except MeasureFailed as exc:
        raise SystemExit(exc.code) from None
    except KeyboardInterrupt:
        print("MEASURE FAILED: interrupted", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
