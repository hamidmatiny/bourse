"""Run inside compose broker: N baseline runs; write summary JSON on mounted ./data."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path

from bourse.broker.runner import broker_from_settings, intake, load_fixture
from bourse.config import get_settings
from bourse.contracts.broker import RunStatus


async def main(runs: int, summary_path: Path) -> int:
    settings = get_settings()
    datasets = ("people", "measurements", "orders")
    statuses: list[str] = []
    aborts: list[str] = []
    ok = 0
    t0 = time.monotonic()
    peak = 0
    metrics: Path | None = None
    per: dict[str, dict] = defaultdict(
        lambda: {"min": None, "max": 0, "last": 0, "runner": "", "ctx": None}
    )
    async with broker_from_settings(settings, echo=print) as session:
        metrics = session.monitor.out
        for i in range(runs):
            ds = datasets[i % len(datasets)]
            csv_text, _ = load_fixture(ds)
            print(f"baseline {i + 1}/{runs} {ds} start", flush=True)
            report = await session.broker.start(
                intake(settings, csv_text, "Profile the dataset, detect PII, normalize units.")
            )
            statuses.append(report.status.value)
            if report.status is RunStatus.SUCCEEDED:
                ok += 1
                print(f"baseline {i + 1}/{runs} SUCCEEDED", flush=True)
            else:
                ar = report.abort_reason or "-"
                aborts.append(f"{report.run_id} {report.status.value} abort_reason={ar}")
                print(f"baseline {i + 1}/{runs} {report.status.value} abort_reason={ar}", flush=True)
                break
        peak = session.monitor.peak
        print(session.monitor.summary(), flush=True)
        print(session.monitor.reload_summary(), flush=True)
        print(f"METRICS_FILE={metrics}", flush=True)
        print(f"BASELINE_OK={ok}/{runs}", flush=True)
        print(f"BASELINE_WALL_S={time.monotonic() - t0:.1f}", flush=True)
        print(f"BASELINE_PEAK={peak}", flush=True)
        if metrics and Path(metrics).exists():
            for line in Path(metrics).read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                for m in row.get("models") or []:
                    n = str(m.get("name") or "")
                    s = int(m.get("size") or 0)
                    per[n]["min"] = s if per[n]["min"] is None else min(per[n]["min"], s)
                    per[n]["max"] = max(per[n]["max"], s)
                    per[n]["last"] = s
                    per[n]["runner"] = m.get("runner") or per[n]["runner"]
                    if m.get("context_length") is not None:
                        per[n]["ctx"] = m["context_length"]
            for n, v in sorted(per.items()):
                print(
                    f"MODEL {n} min={v['min']} max={v['max']} last={v['last']} "
                    f"runner={v['runner']} ctx={v['ctx']}",
                    flush=True,
                )
    wall_s = round(time.monotonic() - t0, 1)
    metrics_host = ""
    if metrics is not None:
        p = Path(metrics)
        metrics_host = str(p).removeprefix("/app/") if str(p).startswith("/app/") else str(p)
    payload = {
        "baseline_ok": ok,
        "baseline_n": runs,
        "wall_s": wall_s,
        "peak": peak,
        "statuses": statuses,
        "aborts": aborts,
        "metrics_file": metrics_host,
        "models": {
            n: {
                "min": v["min"],
                "max": v["max"],
                "last": v["last"],
                "runner": v["runner"],
                "context_length": v["ctx"],
            }
            for n, v in per.items()
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {summary_path}", flush=True)
    return 0 if ok == runs else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--summary", type=Path, default=Path("data/probe/adr-0046-baseline-last.json"))
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.runs, args.summary)))
