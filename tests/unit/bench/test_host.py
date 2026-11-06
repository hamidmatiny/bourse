"""Host conditions (ADR-0051 fix round 2): pmset parsing on captured output, failure recording,
summary n/N, torn host.jsonl line."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

from bourse.bench import host

# Captured on the operator's M3 Max (2026-11-06).
BATT_AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=21954659)\t46%; charging; 1:21 remaining present: true\n"
)
BATT_BAT = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=21954659)\t81%; discharging; 5:02 remaining present: true\n"
)
THERM_NONE = (
    "Note: No thermal warning level has been recorded\n"
    "Note: No performance warning level has been recorded\n"
    "Note: No CPU power status has been recorded\n"
)
THERM_LIMITED = "CPU Power notify\n\tCPU_Scheduler_Limit \t= 100\n\tCPU_Speed_Limit \t= 62\n"
PMSET_G = (
    "System-wide power settings:\nCurrently in use:\n standby              1\n"
    " powermode            {m}\n"
)


def test_parse_captured_pmset_output() -> None:
    assert host.parse_batt(BATT_AC) == ("AC Power", 46, "charging")
    assert host.parse_batt(BATT_BAT) == ("Battery Power", 81, "discharging")
    assert host.parse_powermode(PMSET_G.format(m=1)) == 1
    assert host.parse_powermode("no such line") is None
    assert host.parse_therm(THERM_NONE) == (False, None)
    assert host.parse_therm(THERM_LIMITED) == (True, 62)


def _runner(batt: str, mode: int, therm: str) -> host.Runner:
    def run(args: Sequence[str]) -> str:
        return {"batt": batt, "therm": therm}.get(args[-1], PMSET_G.format(m=mode))

    return run


def test_sample_low_power_on_battery() -> None:
    s = host.sample(
        "during", run=_runner(BATT_BAT, 1, THERM_LIMITED), loadavg=lambda: (3.0, 2.0, 1.0)
    )
    assert (s.power_source, s.battery_pct, s.low_power_mode, s.thermal_warning) == (
        "Battery Power", 81, True, True,
    )  # fmt: skip
    assert s.cpu_speed_limit == 62 and s.load_1m == 3.0 and s.error is None


def test_sample_records_pmset_failure_instead_of_hiding_it() -> None:
    def broken(args: Sequence[str]) -> str:
        raise subprocess.TimeoutExpired(["pmset", *args], 5.0)

    s = host.sample("start", run=broken, loadavg=lambda: (1.0, 1.0, 1.0))
    assert s.error is not None and "TimeoutExpired" in s.error
    assert s.low_power_mode is None and s.load_1m == 1.0


def test_summary_counts_and_torn_line(tmp_path: Path) -> None:
    run = _runner(BATT_AC, 0, THERM_NONE)
    for phase in ("start", "during"):
        host.append(tmp_path, host.sample(phase, run=run, loadavg=lambda: (2.0, 1.0, 1.0)))
    low = _runner(BATT_BAT, 1, THERM_NONE)
    host.append(tmp_path, host.sample("end", run=low, loadavg=lambda: (6.0, 1.0, 1.0)))
    with (tmp_path / host.HOST_FILE).open("a") as fh:
        fh.write('{"at": "2026-')  # killed mid-write
    samples = host.load(tmp_path)
    assert [s.phase for s in samples] == ["start", "during", "end"]
    h = host.summarize_host(samples)
    assert h is not None
    assert (h.on_battery.n, h.on_battery.N, h.low_power.n, h.battery_min_pct) == (1, 3, 1, 46)
    line = host.format_host(h)
    assert "on battery 1/3" in line and "low power mode 1/3" in line and "max 6.00" in line
    assert "not recorded" in host.format_host(host.summarize_host([]))
