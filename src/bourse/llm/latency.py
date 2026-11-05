"""Isolated Ollama latency samples and exam deadline derivation (ADR-0044).

Exam task deadline for a model = max(base_deadline_s, 3 * p95_total_s from probe --latency).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bourse.config import get_settings

LATENCY_DIR = Path("data/probe")
LATENCY_LATEST = LATENCY_DIR / "latency-latest.json"
RELOAD_LOAD_MS = 1000.0  # load_duration > 1s => reload


@dataclass(frozen=True)
class LatencySample:
    total_s: float
    load_s: float
    prompt_eval_s: float
    eval_s: float
    prompt_eval_count: int
    eval_count: int


@dataclass(frozen=True)
class LatencySummary:
    model: str
    n: int
    p50_total_s: float
    p95_total_s: float
    p50_load_s: float
    p95_load_s: float
    p50_eval_s: float
    p95_eval_s: float
    samples: tuple[LatencySample, ...]

    def exam_deadline_s(self, base_s: float) -> float:
        return max(float(base_s), 3.0 * self.p95_total_s)


def _ns_to_s(value: Any) -> float:
    try:
        return float(value or 0) / 1e9
    except (TypeError, ValueError):
        return 0.0


def percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile; `sorted_vals` must be non-empty and sorted ascending."""
    if not sorted_vals:
        raise ValueError("empty sample")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[rank - 1]


def summarize(model: str, samples: list[LatencySample]) -> LatencySummary:
    totals = sorted(s.total_s for s in samples)
    loads = sorted(s.load_s for s in samples)
    evals = sorted(s.eval_s for s in samples)
    return LatencySummary(
        model=model,
        n=len(samples),
        p50_total_s=percentile(totals, 50),
        p95_total_s=percentile(totals, 95),
        p50_load_s=percentile(loads, 50),
        p95_load_s=percentile(loads, 95),
        p50_eval_s=percentile(evals, 50),
        p95_eval_s=percentile(evals, 95),
        samples=tuple(samples),
    )


async def sample_chat(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    *,
    num_ctx: int | None = None,
    num_predict: int = 32,
    keep_alive: str | None = None,
) -> LatencySample:
    """One isolated /api/chat (think=false); timings from Ollama response fields."""
    settings = get_settings()
    if num_ctx is None:
        num_ctx = settings.num_ctx_for(model)
    if keep_alive is None:
        keep_alive = settings.bourse_meter_keep_alive
    started = time.monotonic()
    resp = await client.post(
        f"{base_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK"}],
            "stream": False,
            "think": False,
            "keep_alive": keep_alive,
            "options": {"num_ctx": num_ctx, "num_predict": num_predict},
        },
    )
    wall = time.monotonic() - started
    resp.raise_for_status()
    data = resp.json()
    total = _ns_to_s(data.get("total_duration"))
    if total <= 0:
        total = wall
    return LatencySample(
        total_s=total,
        load_s=_ns_to_s(data.get("load_duration")),
        prompt_eval_s=_ns_to_s(data.get("prompt_eval_duration")),
        eval_s=_ns_to_s(data.get("eval_duration")),
        prompt_eval_count=int(data.get("prompt_eval_count") or 0),
        eval_count=int(data.get("eval_count") or 0),
    )


async def measure_latency(
    client: httpx.AsyncClient,
    base_url: str,
    models: list[str],
    n: int = 5,
) -> dict[str, LatencySummary]:
    out: dict[str, LatencySummary] = {}
    for model in models:
        samples: list[LatencySample] = []
        for _ in range(n):
            samples.append(await sample_chat(client, base_url, model))
        out[model] = summarize(model, samples)
    return out


def write_latency_report(
    summaries: dict[str, LatencySummary],
    *,
    base_url: str,
    base_deadline_s: float,
    out_dir: Path = LATENCY_DIR,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload: dict[str, Any] = {
        "at": stamp,
        "base_url": base_url,
        "base_deadline_s": base_deadline_s,
        "models": {},
    }
    for model, s in summaries.items():
        payload["models"][model] = {
            "n": s.n,
            "p50_total_s": s.p50_total_s,
            "p95_total_s": s.p95_total_s,
            "p50_load_s": s.p50_load_s,
            "p95_load_s": s.p95_load_s,
            "p50_eval_s": s.p50_eval_s,
            "p95_eval_s": s.p95_eval_s,
            "exam_deadline_s": s.exam_deadline_s(base_deadline_s),
            "samples": [
                {
                    "total_s": x.total_s,
                    "load_s": x.load_s,
                    "prompt_eval_s": x.prompt_eval_s,
                    "eval_s": x.eval_s,
                    "prompt_eval_count": x.prompt_eval_count,
                    "eval_count": x.eval_count,
                }
                for x in s.samples
            ],
        }
    path = out_dir / f"latency-{stamp}.json"
    text = json.dumps(payload, indent=2) + "\n"
    path.write_text(text)
    LATENCY_LATEST.write_text(text)
    return path


def load_deadline_by_model(
    path: Path | None = None,
    *,
    base_deadline_s: float | None = None,
) -> dict[str, float]:
    """Return model -> max(base, 3*p95_total_s) from latency-latest.json (empty if missing)."""
    target = path or LATENCY_LATEST
    if not target.is_file():
        return {}
    data = json.loads(target.read_text())
    base = float(
        base_deadline_s
        if base_deadline_s is not None
        else data.get("base_deadline_s") or get_settings().bourse_exam_deadline_s
    )
    out: dict[str, float] = {}
    for model, row in (data.get("models") or {}).items():
        p95 = float(row.get("p95_total_s") or 0)
        if p95 > 0:
            out[str(model)] = max(base, 3.0 * p95)
        elif "exam_deadline_s" in row:
            out[str(model)] = max(base, float(row["exam_deadline_s"]))
    return out


def ns_to_ms(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1e6
    except (TypeError, ValueError):
        return None


def timings_from_ollama(data: dict[str, Any]) -> dict[str, Any]:
    """Map Ollama /api/chat timing fields to llm_usage columns (ms) + reload flag."""
    load_ms = ns_to_ms(data.get("load_duration"))
    return {
        "total_duration_ms": ns_to_ms(data.get("total_duration")),
        "load_duration_ms": load_ms,
        "prompt_eval_duration_ms": ns_to_ms(data.get("prompt_eval_duration")),
        "eval_duration_ms": ns_to_ms(data.get("eval_duration")),
        "reload": bool(load_ms is not None and load_ms > RELOAD_LOAD_MS),
    }
