"""Copy the final bench outputs into the tracked results/ dir (ADR-0054).

data/ is never committed. RESULTS.md must be reproducible from the repo, so this copies, per
final segment, the files the bench wrote that hold no secrets (summary.json, results.jsonl,
provenance.json) and reduces the segment's database export (data/bench/<name>/evidence/*.csv)
to counts (evidence.json). Violation evidence text, honeytokens, agent outputs and Slack user
ids are not copied.

Refuses unless every final segment is final=True, dirty=False, on one commit, one manifest and
one model digest.
"""

import csv
import json
import platform
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from bourse.contracts.bench import BenchSummary, Provenance
from bourse.contracts.results import (
    EvidenceDigest,
    HostHardware,
    LedgerCount,
    Phase5cEvidence,
    ScenarioLine,
    SelectionCount,
    SelectionTerms,
    ViolationCount,
)

# The five final blocks (ADR-0051 schedule; block 3 is the clean re-run, ADR-0054).
FINAL_SEGMENTS: tuple[str, ...] = (
    "final-suite",
    "final-b3-mix",
    "final-b3",
    "final-b1-mix",
    "final-b3-no-spotcheck",
    "final-b3-no-canary",
    "final-no-sinkhole",
    "final-no-cardsig",
    "final-b1-b0",
    "final-no-exam",
    "final-no-metercap",
)
# Dev-split supporting evidence, reported as dev: the executor speedup (ADR-0051).
SUPPORTING_SEGMENTS: tuple[str, ...] = ("pilot-6a",)
COPIED = ("summary.json", "results.jsonl", "provenance.json")
PII_SKILL = "pii.detect"


class ExportRefused(RuntimeError):
    pass


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ExportRefused(f"missing evidence file {path}")
    csv.field_size_limit(1 << 26)
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _spot(value: str) -> bool | None:
    return {"t": True, "f": False, "true": True, "false": False}.get(value.strip().lower())


def _opt(v: Any, kind: type[int] | type[float]) -> Any:
    return None if v is None else kind(v)


def evidence_digest(segment: str, evidence_dir: Path) -> EvidenceDigest:
    """Counts from violations.csv, ledger_entries.csv and selection_decisions.csv. A segment
    without an evidence/ dir gets `available=False` and empty counts."""
    if not evidence_dir.is_dir():
        return EvidenceDigest(
            segment=segment,
            available=False,
            ledger_rows=0,
            violations=(),
            ledger=(),
            selections=(),
            last_pii_selection=(),
        )
    violations = Counter(
        (r["agent_id"], r["kind"]) for r in _csv_rows(evidence_dir / "violations.csv")
    )
    ledger_rows = _csv_rows(evidence_dir / "ledger_entries.csv")
    ledger = Counter(
        (r["agent_id"], r["skill"], r["kind"], r["state"], _spot(r["spot_check_passed"]))
        for r in ledger_rows
    )
    decisions = _csv_rows(evidence_dir / "selection_decisions.csv")
    selections = Counter((r["skill"], r["chosen"] or None, r["reason"]) for r in decisions)
    pii = [r for r in decisions if r["skill"] == PII_SKILL]
    last: list[SelectionTerms] = []
    if pii:
        final = max(pii, key=lambda r: (r["at"], int(r["id"])))
        for cand in json.loads(final["candidates"]):
            t = cand.get("terms") or {}
            last.append(
                SelectionTerms(
                    agent_id=cand["agent_id"],
                    s=_opt(t.get("s"), int),
                    f=_opt(t.get("f"), int),
                    reliability_lb=_opt(t.get("reliability_lb"), float),
                    latency_ms=_opt(t.get("latency"), float),
                    cost_norm=_opt(t.get("cost_norm"), float),
                    utility=_opt(t.get("utility"), float),
                )
            )
    return EvidenceDigest(
        segment=segment,
        ledger_rows=len(ledger_rows),
        violations=tuple(
            ViolationCount(agent_id=a, kind=k, n=n) for (a, k), n in sorted(violations.items())
        ),
        ledger=tuple(
            LedgerCount(agent_id=a, skill=s, kind=k, state=st, spot_check_passed=sp, n=n)
            for (a, s, k, st, sp), n in sorted(ledger.items(), key=lambda kv: str(kv[0]))
        ),
        selections=tuple(
            SelectionCount(skill=s, chosen=c, reason=r, n=n)
            for (s, c, r), n in sorted(selections.items(), key=lambda kv: str(kv[0]))
        ),
        last_pii_selection=tuple(last),
    )


def check_provenance(provs: dict[str, Provenance]) -> Provenance:
    """All final segments: final, clean, one commit / manifest / model digest."""
    bad = [n for n, p in provs.items() if not p.final or p.git_dirty]
    if bad:
        raise ExportRefused(f"not final or dirty: {', '.join(bad)}")
    for field in ("git_sha", "manifest_sha256", "model_digests"):
        values = {json.dumps(getattr(p, field), sort_keys=True) for p in provs.values()}
        if len(values) != 1:
            raise ExportRefused(f"final segments disagree on {field}: {sorted(values)}")
    return next(iter(provs.values()))


_SCENARIO = re.compile(
    r"(?P<adv>A\d+\w*)/(?P<mode>[\w-]+): exposure=(?P<exp>\d+) \(real (?P<real>\d+), "
    r"canaries (?P<can>\d+)\) first_bad=(?P<fb>\S+) detect_delay=(?P<dd>\S+) "
    r"strikes=(?P<st>\d+) rel_epoch=(?P<ep>\d+)"
)
_STATUS = re.compile(r"^(A\d+\w*)\s+(\S+)\s+(caught|missed|ineffective|error)\b", re.M)


def _opt_int(v: str) -> int | None:
    return None if v in ("None", "-", "") else int(v)


def _scenario(text: str) -> ScenarioLine:
    m = _SCENARIO.search(text)
    s = _STATUS.search(text)
    if m is None or s is None:
        raise ExportRefused("phase 5c log has no scenario line")
    return ScenarioLine(
        adversary=m["adv"],
        mode=m["mode"],
        status=s.group(3),
        exposure=int(m["exp"]),
        exposure_real=int(m["real"]),
        canaries=int(m["can"]),
        first_bad=_opt_int(m["fb"]),
        detect_delay=_opt_int(m["dd"]),
        strikes=int(m["st"]),
        rel_epoch=int(m["ep"]),
    )


def _field(text: str, pattern: str) -> re.Match[str]:
    m = re.search(pattern, text)
    if m is None:
        raise ExportRefused(f"phase 5c log: no match for {pattern!r}")
    return m


def phase5c_evidence(log_dir: Path) -> Phase5cEvidence:
    read = {p.stem: p.read_text() for p in log_dir.glob("*.log")}
    need = {"a5a", "recatch", "approve", "deny", "slack-approve", "slack-timeout"}
    if missing := need - read.keys():
        raise ExportRefused(f"phase 5c logs missing: {sorted(missing)}")
    approve = _field(read["approve"], r"exam=(\d+/\d+) approved=True .*to_state=(\w+)")
    epoch = _field(read["approve"], r"rel_epoch (\d+) -> (\d+)")
    deny = _field(read["deny"], r"exam=(\d+/\d+) approved=False .*to_state=(\w+)")
    sa = _field(read["slack-approve"], r"approved=(\w+) by=\S+ reason=([^\n]+?) at=")
    st = _field(read["slack-timeout"], r"approved=(\w+) by=\S+ reason=([^\n]+?) at=")
    return Phase5cEvidence(
        catch=_scenario(read["a5a"]),
        recatch=_scenario(read["recatch"]),
        approve_exam=approve[1],
        approve_to_state=approve[2],
        approve_epoch_from=int(epoch[1]),
        approve_epoch_to=int(epoch[2]),
        deny_exam=deny[1],
        deny_to_state=deny[2],
        slack_approve=f"approved={sa[1]} reason={sa[2]}",
        slack_timeout=f"approved={st[1]} reason={st[2]}",
    )


def _cmd(args: Sequence[str]) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def host_hardware(ollama_url: str) -> HostHardware:
    mem = _cmd(["sysctl", "-n", "hw.memsize"])
    ncpu = _cmd(["sysctl", "-n", "hw.ncpu"])
    try:
        ollama = httpx.get(f"{ollama_url}/api/version", timeout=3.0).json().get("version")
    except (httpx.HTTPError, ValueError):
        ollama = None
    mac = platform.mac_ver()[0]
    return HostHardware(
        model=_cmd(["sysctl", "-n", "hw.model"]),
        cpu=_cmd(["sysctl", "-n", "machdep.cpu.brand_string"]),
        cpu_count=int(ncpu) if ncpu and ncpu.isdigit() else None,
        memory_bytes=int(mem) if mem and mem.isdigit() else None,
        os=f"macOS {mac}" if mac else platform.platform(),
        ollama_version=ollama,
        docker_version=_cmd(["docker", "--version"]),
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")


def export_results(
    bench_dir: Path,
    phase5c_logs: Path,
    out_dir: Path,
    *,
    ollama_url: str,
    segments: Sequence[str] = FINAL_SEGMENTS,
) -> list[str]:
    """Returns one line per file written."""
    provs: dict[str, Provenance] = {}
    for name in segments:
        src = bench_dir / name
        for f in COPIED:
            if not (src / f).exists():
                raise ExportRefused(f"{src / f} missing")
        provs[name] = BenchSummary.model_validate_json(
            (src / "summary.json").read_text()
        ).provenance
    check_provenance(provs)
    lines: list[str] = []
    for name in segments:
        src, dst = bench_dir / name, out_dir / name
        dst.mkdir(parents=True, exist_ok=True)
        for f in COPIED:
            shutil.copyfile(src / f, dst / f)
            lines.append(f"{dst / f}")
        digest = evidence_digest(name, src / "evidence")
        _write(dst / "evidence.json", digest.model_dump_json(indent=1))
        state = f"ledger={digest.ledger_rows}" if digest.available else "no evidence export"
        lines.append(f"{dst / 'evidence.json'} {state}")
    for name in SUPPORTING_SEGMENTS:
        src, dst = bench_dir / name, out_dir / "supporting" / name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "summary.json", dst / "summary.json")
        lines.append(f"{dst / 'summary.json'}")
    p5 = phase5c_evidence(phase5c_logs)
    _write(out_dir / "supporting" / "phase5c.json", p5.model_dump_json(indent=1))
    lines.append(f"{out_dir / 'supporting' / 'phase5c.json'}")
    hw = host_hardware(ollama_url)
    _write(out_dir / "host.json", hw.model_dump_json(indent=1))
    lines.append(f"{out_dir / 'host.json'}")
    return lines
