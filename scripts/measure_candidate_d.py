#!/usr/bin/env python3
"""Throwaway candidate d: all 5 agents on qwen3.5:27b-q4_K_M, NUM_PARALLEL=2.

Unloads gemma (keep_alive=0) and verifies /api/ps before exams. No committed roster change.
Restores ADR-0021 roster + meter gate parallel=2 on exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# Allow `uv run python scripts/measure_candidate_d.py` (no scripts package install).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx

from bourse.agents.catalog import ROSTER_LARGE_MODEL
from bourse.config import get_settings
from bourse.contracts.exam import ExamReport
from bourse.runtime.resident import RESIDENT_LIMIT_BYTES, format_bytes_units
from bourse.stack.activation import print_exam_summary_table, registry_agent_rows
from scripts.measure_resident_candidates import (
    ALL_AGENT_KEYS,
    OUT,
    MeasureFailed,
    _agent_ids,
    _analyze_metrics,
    _ensure_registry,
    _fetch_report,
    patch_all_env_parallel,
    restore_roster_env,
    run_baselines,
    run_exams_with_peak,
    reregister,
    set_all_agents_model,
    set_meter_parallel,
    unload_model,
    verify_ps_models,
)

QWEN27 = ROSTER_LARGE_MODEL  # qwen3.5:27b-q4_K_M
CARD_VERSION = "1.0.1"  # semver bump required; prerelease tags count as VERSION_ROLLBACK
D_OUT = Path("data/probe/adr-0046-candidate-d.json")


def _classify_failure(outcome: str, detail: str) -> str:
    if outcome == "EXAM_TIMEOUT":
        return "EXAM_TIMEOUT"
    if outcome == "grader-failed":
        return f"grader-failed + {detail}" if detail else "grader-failed"
    if outcome == "ERROR":
        ek = ""
        for part in detail.split(";"):
            part = part.strip()
            if part.startswith("error_kind="):
                ek = part.split("=", 1)[1]
                break
        return f"agent FAILED + error_kind={ek or '?'}"
    return f"{outcome}:{detail}" if detail else outcome


def _usage_for_tasks(task_ids: list[str]) -> dict[str, dict]:
    if not task_ids:
        return {}
    quoted = ",".join("'" + t.replace("'", "''") + "'" for t in task_ids)
    q = f"""
SELECT task_id,
       coalesce(sum(wall_ms),0)::bigint,
       coalesce(sum(queue_wait_ms),0)::bigint,
       coalesce(sum(tokens_in),0)::bigint,
       coalesce(sum(tokens_out),0)::bigint,
       max(model)
FROM llm_usage
WHERE task_id IN ({quoted})
GROUP BY 1;
"""
    proc = subprocess.run(
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
            "-t",
            "-A",
            "-F",
            "|",
            "-c",
            q,
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MeasureFailed(f"llm_usage query failed: {(proc.stderr or '')[:300]}")
    out: dict[str, dict] = {}
    for line in (proc.stdout or "").splitlines():
        if not line.strip() or "|" not in line:
            continue
        tid, wall, qwait, tin, tout, model = line.split("|", 5)
        out[tid] = {
            "wall_ms": int(wall),
            "queue_wait_ms": int(qwait),
            "tokens_in": int(tin),
            "tokens_out": int(tout),
            "model": model,
        }
    return out


def _tokens_by_agent(since_iso: str) -> dict[str, dict]:
    q = f"""
SELECT agent_id,
       coalesce(sum(tokens_in),0)::bigint,
       coalesce(sum(tokens_out),0)::bigint,
       count(*)::bigint
FROM llm_usage
WHERE at >= '{since_iso}'::timestamptz
  AND agent_id LIKE 'vendor-%'
GROUP BY 1
ORDER BY 1;
"""
    proc = subprocess.run(
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
            "-t",
            "-A",
            "-F",
            "|",
            "-c",
            q,
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MeasureFailed(f"tokens query failed: {(proc.stderr or '')[:300]}")
    out: dict[str, dict] = {}
    for line in (proc.stdout or "").splitlines():
        if not line.strip() or "|" not in line:
            continue
        aid, tin, tout, n = line.split("|", 3)
        out[aid] = {"tokens_in": int(tin), "tokens_out": int(tout), "n_calls": int(n)}
    return out


def _exam_failures(reports: dict[str, ExamReport | None]) -> list[dict]:
    fails: list[dict] = []
    task_ids: list[str] = []
    for aid, rep in reports.items():
        if rep is None:
            continue
        for t in rep.tasks:
            if t.outcome == "passed":
                continue
            task_ids.append(t.task_id)
            fails.append(
                {
                    "agent": aid,
                    "model": rep.model,
                    "skill": t.skill,
                    "task_id": t.task_id,
                    "outcome": t.outcome,
                    "failure_type": _classify_failure(t.outcome, t.detail),
                    "detail": t.detail,
                }
            )
    usage = _usage_for_tasks(task_ids)
    for f in fails:
        u = usage.get(f["task_id"], {})
        f["wall_ms"] = u.get("wall_ms")
        f["queue_wait_ms_sum"] = u.get("queue_wait_ms")
        if u.get("model"):
            f["model"] = u["model"]
    return fails


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        _ensure_registry()
        print("=== restore default meter gate parallel=2 (host operator-restarted) ===", flush=True)
        patch_all_env_parallel(2)
        set_meter_parallel(2)

        print("=== unload gemma; verify /api/ps ===", flush=True)
        unload_model("gemma4:12b")
        # also drop any leftover 4b from prior throwaway
        unload_model("qwen3.5:4b")
        time.sleep(1.0)
        verify_ps_models(forbidden={"gemma4:12b", "qwen3.5:4b"})

        print("=== candidate d: all 5 agents qwen27b, parallel=2 ===", flush=True)
        set_all_agents_model(QWEN27, card_version=CARD_VERSION)
        reregister()

        # Before exams, ensure gemma still gone (agents may not have loaded yet)
        verify_ps_models(forbidden={"gemma4:12b"})

        exam_wall, exam_peak, scores = run_exams_with_peak(ALL_AGENT_KEYS)
        # After exams: only qwen27b should be resident (or empty if unloaded)
        live = verify_ps_models(forbidden={"gemma4:12b"})
        if live and any(n != QWEN27 for n in live):
            raise MeasureFailed(f"expected only {QWEN27} after exams, got {live}")

        settings = get_settings()
        ids = _agent_ids()
        base = settings.bourse_registry_url.rstrip("/")
        reports: dict[str, ExamReport | None] = {}
        with httpx.Client(timeout=30.0) as client:
            rows = registry_agent_rows(client, base)
            focus = [(a, s, last) for a, s, last in rows if a in set(ids.values())]
            print_exam_summary_table(client, base, focus, echo=print)
            for aid in ids.values():
                reports[aid] = _fetch_report(client, base, aid)

        failures = _exam_failures(reports)
        print(f"\n=== d failed exam tasks ({len(failures)}) ===", flush=True)
        by_type: dict[str, int] = defaultdict(int)
        for f in failures:
            key = (
                "grader-failed"
                if f["failure_type"].startswith("grader-failed")
                else f["failure_type"]
            )
            by_type[key] += 1
            print(
                f"agent={f['agent']} model={f['model']} skill={f['skill']} "
                f"type={f['failure_type']} wall_ms={f['wall_ms']} "
                f"queue_wait_ms_sum={f['queue_wait_ms_sum']}",
                flush=True,
            )
            print(f"  detail={f['detail'][:240]}", flush=True)
        print("-- d failed by type --", flush=True)
        for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}", flush=True)

        base_wall, ok, statuses, aborts, metrics, base_peak = run_baselines(3)
        file_peak, model_peaks = _analyze_metrics(metrics)
        combined = max(exam_peak, base_peak, file_peak)

        tokens = _tokens_by_agent(started_at)
        print("\n=== tokens in/out per agent (since measure start) ===", flush=True)
        for aid, t in sorted(tokens.items()):
            print(
                f"  {aid} tokens_in={t['tokens_in']} tokens_out={t['tokens_out']} n_calls={t['n_calls']}",
                flush=True,
            )
        # highlight pii-fast vs pii-precise
        pf = next((t for a, t in tokens.items() if "pii-fast" in a), None)
        pp = next((t for a, t in tokens.items() if "pii-precise" in a), None)
        print(
            f"pii-fast vs pii-precise: "
            f"fast_in={pf and pf['tokens_in']} fast_out={pf and pf['tokens_out']} | "
            f"precise_in={pp and pp['tokens_in']} precise_out={pp and pp['tokens_out']}",
            flush=True,
        )

        payload = {
            "candidate_id": "d-qwen27b-all-parallel2",
            "description": (
                f"all 5 agents {QWEN27}; host+meter OLLAMA_NUM_PARALLEL=2; "
                "gemma unloaded keep_alive=0 before exams"
            ),
            "meter_parallel": 2,
            "model": QWEN27,
            "exam_peak_bytes": exam_peak,
            "baseline_peak_bytes": max(base_peak, file_peak),
            "combined_peak_bytes": combined,
            "exam_wall_s": round(exam_wall, 1),
            "baseline_wall_s": round(base_wall, 1),
            "baseline_ok": ok,
            "baseline_n": 3,
            "baseline_statuses": statuses,
            "abort_reasons": aborts,
            "exam_scores": [asdict(s) for s in scores],
            "model_peaks": [asdict(m) for m in model_peaks],
            "metrics_file": str(metrics),
            "exam_failures": failures,
            "failed_by_type": dict(by_type),
            "tokens_by_agent": tokens,
            "limit_bytes": RESIDENT_LIMIT_BYTES,
            "limit_human": format_bytes_units(RESIDENT_LIMIT_BYTES),
            "notes": (
                f"host OLLAMA_NUM_PARALLEL assumed=2; meter gate=2; "
                f"combined_peak={combined} ({format_bytes_units(combined)})"
            ),
        }
        D_OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(
            f"d combined_peak={combined} ({format_bytes_units(combined)}) "
            f"baseline={ok}/3 exam_wall={exam_wall:.1f}s baseline_wall={base_wall:.1f}s",
            flush=True,
        )
        for mp in model_peaks:
            print(
                f"  model {mp.name} min={mp.min_size} max={mp.max_size} last={mp.last_size} "
                f"runner={mp.runner} ctx={mp.context_length}",
                flush=True,
            )
        print("wrote", D_OUT, flush=True)

        # merge into candidates json
        prev = json.loads(OUT.read_text()) if OUT.exists() else {"candidates": []}
        cands = [c for c in prev.get("candidates", []) if not str(c.get("candidate_id", "")).startswith("d-")]
        cands.append({k: v for k, v in payload.items() if k != "exam_failures"})
        # keep failures in d file; slim entry in candidates
        slim = {k: v for k, v in payload.items() if k not in {"exam_failures"}}
        cands[-1] = slim
        OUT.write_text(
            json.dumps(
                {
                    "limit_bytes": RESIDENT_LIMIT_BYTES,
                    "limit_human": format_bytes_units(RESIDENT_LIMIT_BYTES),
                    "candidates": cands,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("wrote", OUT, flush=True)
        return 0
    except MeasureFailed:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MeasureFailed(f"{type(exc).__name__}: {exc}") from None
    finally:
        try:
            restore_roster_env(parallel=2)
            patch_all_env_parallel(2)
        except Exception as exc:  # noqa: BLE001
            print(f"restore warning: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MeasureFailed as exc:
        raise SystemExit(exc.code) from None
    except KeyboardInterrupt:
        print("MEASURE FAILED: interrupted", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
