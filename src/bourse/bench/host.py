"""Host conditions for bench runs (ADR-0051 fix round 2).

The PII re-run `pii-identity-6a` was ~1.8x slower than the pilot with no recorded reason. Every
run started with `bourse bench launch` samples macOS power state (pmset -g batt / -g / -g therm)
and the load average at start, every `interval` seconds, and at the end, into
data/bench/<name>/host.jsonl. The summary reports them as n/N samples. Parsing is pure and
tested on captured pmset output; a missing or failing pmset is recorded as `error`, not hidden.
"""

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from bourse.bench.stats import quantile, wilson
from bourse.contracts.bench import HostSample, HostSummary

HOST_FILE = "host.jsonl"
PMSET_TIMEOUT_S = 5.0

_BATT_SOURCE = re.compile(r"Now drawing from '([^']+)'")
_BATT_PCT = re.compile(r"\t(\d+)%;\s*([^;]+);")
_POWERMODE = re.compile(r"^\s*powermode\s+(\d+)", re.MULTILINE)
_SPEED = re.compile(r"CPU_Speed_Limit\s*=\s*(\d+)")


def parse_batt(text: str) -> tuple[str | None, int | None, str | None]:
    src = _BATT_SOURCE.search(text)
    pct = _BATT_PCT.search(text)
    return (
        src.group(1) if src else None,
        int(pct.group(1)) if pct else None,
        pct.group(2).strip() if pct else None,
    )


def parse_powermode(pmset_g: str) -> int | None:
    """`pmset -g` lists the settings currently in use; `powermode` 1 is Low Power Mode."""
    m = _POWERMODE.search(pmset_g)
    return int(m.group(1)) if m else None


def parse_therm(text: str) -> tuple[bool, int | None]:
    """(warning, CPU_Speed_Limit). "Note: No ... has been recorded" lines mean no warning."""
    speed = _SPEED.search(text)
    limit = int(speed.group(1)) if speed else None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    warning = any(
        not ln.startswith("Note: No") and ("warning" in ln.lower() or "level" in ln.lower())
        for ln in lines
    )
    return warning or (limit is not None and limit < 100), limit


Runner = Callable[[Sequence[str]], str]


def _pmset(args: Sequence[str]) -> str:
    out = subprocess.run(
        ["/usr/bin/pmset", *args],
        capture_output=True,
        text=True,
        timeout=PMSET_TIMEOUT_S,
        check=True,
    )
    return out.stdout


def sample(
    phase: Literal["start", "during", "end"],
    *,
    run: Runner = _pmset,
    loadavg: Callable[[], tuple[float, float, float]] = os.getloadavg,
) -> HostSample:
    l1, l5, l15 = loadavg()
    base = {"at": datetime.now(UTC), "phase": phase, "load_1m": l1, "load_5m": l5, "load_15m": l15}
    try:
        source, pct, state = parse_batt(run(["-g", "batt"]))
        mode = parse_powermode(run(["-g"]))
        warning, limit = parse_therm(run(["-g", "therm"]))
    except (OSError, subprocess.SubprocessError) as exc:
        return HostSample.model_validate(
            base
            | {
                "power_source": None,
                "battery_pct": None,
                "battery_state": None,
                "powermode": None,
                "low_power_mode": None,
                "thermal_warning": None,
                "cpu_speed_limit": None,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }
        )
    return HostSample.model_validate(
        base
        | {
            "power_source": source,
            "battery_pct": pct,
            "battery_state": state,
            "powermode": mode,
            "low_power_mode": None if mode is None else mode == 1,
            "thermal_warning": warning,
            "cpu_speed_limit": limit,
        }
    )


def append(run_dir: Path, s: HostSample) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / HOST_FILE).open("a") as fh:
        fh.write(s.model_dump_json() + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def load(run_dir: Path) -> list[HostSample]:
    path = run_dir / HOST_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(HostSample.model_validate_json(line))
            except ValueError:
                continue  # torn last line from a killed launcher
    return out


def summarize_host(samples: Sequence[HostSample]) -> HostSummary | None:
    if not samples:
        return None
    n = len(samples)
    batt = [s.battery_pct for s in samples if s.battery_pct is not None]
    limits = [s.cpu_speed_limit for s in samples if s.cpu_speed_limit is not None]
    loads = [s.load_1m for s in samples]
    return HostSummary(
        samples=n,
        on_battery=wilson(sum(s.power_source == "Battery Power" for s in samples), n),
        low_power=wilson(sum(s.low_power_mode is True for s in samples), n),
        thermal_warning=wilson(sum(s.thermal_warning is True for s in samples), n),
        battery_min_pct=min(batt) if batt else None,
        cpu_speed_limit_min=min(limits) if limits else None,
        load_1m_p50=quantile(loads, 0.5).value,
        load_1m_max=max(loads),
    )


def format_host(h: HostSummary | None) -> str:
    if h is None:
        return "host conditions: not recorded (start runs with `bourse bench launch`)"
    return (
        f"host conditions ({h.samples} samples): on battery {h.on_battery.n}/{h.on_battery.N}, "
        f"low power mode {h.low_power.n}/{h.low_power.N}, "
        f"thermal warning {h.thermal_warning.n}/{h.thermal_warning.N}, "
        f"battery min {'-' if h.battery_min_pct is None else f'{h.battery_min_pct}%'}, "
        f"CPU speed limit min "
        f"{'-' if h.cpu_speed_limit_min is None else f'{h.cpu_speed_limit_min}%'}, "
        f"load1 p50 {_num(h.load_1m_p50)} max {_num(h.load_1m_max)}"
    )


def _num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"
