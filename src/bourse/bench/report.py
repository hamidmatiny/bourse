"""Aggregate bench records into n/N metrics with CIs, speedups and a sizing table (ADR-0051)."""

import math
from collections import defaultdict
from collections.abc import Sequence

from bourse.bench.host import format_host, summarize_host
from bourse.bench.stats import quantile, wilson, wilson_half_width
from bourse.contracts.bench import (
    AdversaryResult,
    AdversarySummary,
    BenchConfig,
    BenchRecord,
    BenchSummary,
    CellResult,
    ConfigSpec,
    ConfigSummary,
    HostSample,
    PiiAgentSummary,
    PiiCaseResult,
    Proportion,
    Provenance,
    Speedup,
)

ADVERSARY_STATUSES = ("caught", "missed", "not-exposed", "error")


def summarize_config(config_id: str, cells: Sequence[CellResult]) -> ConfigSummary:
    n = len(cells)
    tokens = [float(c.tokens_in + c.tokens_out) for c in cells]
    compute = [c.compute_ms / 1000 for c in cells]
    wall = [c.wall_s for c in cells]
    subtasks = [s for c in cells for s in c.subtasks]
    return ConfigSummary(
        config_id=config_id,
        task_success=wilson(sum(c.status == "SUCCEEDED" for c in cells), n),
        grader_pass=wilson(sum(c.all_passed for c in cells), n),
        subtask_pass=wilson(sum(s.passed for s in subtasks), len(subtasks)),
        contention=wilson(sum(c.contention_events > 0 for c in cells), n),
        contention_events=sum(c.contention_events for c in cells),
        tokens_p50=quantile(tokens, 0.5),
        tokens_p95=quantile(tokens, 0.95),
        compute_s_p50=quantile(compute, 0.5),
        compute_s_p95=quantile(compute, 0.95),
        latency_s_p50=quantile(wall, 0.5),
        latency_s_p95=quantile(wall, 0.95),
        wall_s_total=sum(wall),
    )


def summarize_adversaries(rows: Sequence[AdversaryResult]) -> AdversarySummary | None:
    scored = [r for r in rows if r.status in ADVERSARY_STATUSES]
    if not rows:
        return None
    n = len(scored)
    return AdversarySummary(
        caught=wilson(sum(r.status == "caught" for r in scored), n),
        missed=wilson(sum(r.status == "missed" for r in scored), n),
        not_exposed=wilson(sum(r.status == "not-exposed" for r in scored), n),
        error=wilson(sum(r.status == "error" for r in scored), n),
        harmed=wilson(sum(r.harmed for r in rows), sum(r.harmed_total for r in rows)),
    )


def summarize_pii(rows: Sequence[PiiCaseResult]) -> tuple[PiiAgentSummary, ...]:
    by_agent: dict[str, list[PiiCaseResult]] = defaultdict(list)
    for r in rows:
        by_agent[r.agent_id].append(r)
    out = []
    for agent, rs in sorted(by_agent.items()):
        metered = [r for r in rs if r.tokens_in is not None and r.tokens_out is not None]
        tokens = [float((r.tokens_in or 0) + (r.tokens_out or 0)) for r in metered]
        budgeted = [r for r in metered if r.task_max_tokens is not None]
        out.append(
            PiiAgentSummary(
                agent_id=agent,
                passed=wilson(sum(r.passed for r in rs), len(rs)),
                f1_p50=quantile([r.f1 for r in rs], 0.5),
                completed=wilson(sum(r.state == "COMPLETED" for r in rs), len(rs)),
                tokens_p50=quantile(tokens, 0.5) if metered else None,
                tokens_max=int(max(tokens)) if metered else None,
                calls_p50=quantile([float(r.llm_calls or 0) for r in metered], 0.5)
                if metered
                else None,
                wall_s_p50=quantile([r.wall_s for r in rs], 0.5),
                over_budget=wilson(
                    sum(
                        (r.tokens_in or 0) + (r.tokens_out or 0) > (r.task_max_tokens or 0)
                        for r in budgeted
                    ),
                    len(budgeted),
                )
                if budgeted
                else None,
            )
        )
    return tuple(out)


def pii_identical(rows: Sequence[PiiCaseResult]) -> Proportion | None:
    """Cases where all PII agents returned byte-identical output (equal output_sha256)."""
    agents = {r.agent_id for r in rows}
    if len(agents) < 2:
        return None
    by_case: dict[str, dict[str, str | None]] = defaultdict(dict)
    for r in rows:
        by_case[r.case_id][r.agent_id] = r.output_sha256
    hashed = [
        set(h.values())
        for h in by_case.values()
        if set(h) == agents and all(v is not None for v in h.values())
    ]
    return wilson(sum(len(h) == 1 for h in hashed), len(hashed))


def speedups(specs: Sequence[ConfigSpec], cells: Sequence[CellResult]) -> tuple[Speedup, ...]:
    """Same baseline+ablation at subtask_slots 1 vs > 1, paired on (case, repeat)."""
    by_config: dict[str, dict[tuple[str, int], CellResult]] = defaultdict(dict)
    for c in cells:
        by_config[c.config_id][(c.case_id, c.repeat)] = c
    out = []
    for serial in specs:
        if serial.subtask_slots != 1:
            continue
        for par in specs:
            if (par.baseline, par.ablation) != (serial.baseline, serial.ablation):
                continue
            if par.subtask_slots <= 1:
                continue
            a, b = by_config.get(serial.id, {}), by_config.get(par.id, {})
            pairs = sorted(set(a) & set(b))
            ws = sum(a[k].wall_s for k in pairs)
            wp = sum(b[k].wall_s for k in pairs)
            ratios = [a[k].wall_s / b[k].wall_s for k in pairs if b[k].wall_s > 0]
            q = quantile(ratios, 0.5)
            out.append(
                Speedup(
                    serial_config=serial.id,
                    parallel_config=par.id,
                    paired_cases=len(pairs),
                    wall_serial_s=ws,
                    wall_parallel_s=wp,
                    total_ratio=(ws / wp) if wp > 0 else None,
                    paired_ratio_p50=q.value,
                )
            )
    return tuple(out)


CONFIG_COLUMNS = (
    "config",
    "N",
    "task_success n/N [95% CI]",
    "grader_pass n/N [CI]",
    "subtask_pass n/N [CI]",
    "contention n/N (events)",
    "tokens p50",
    "tokens p95",
    "compute_s p50",
    "compute_s p95",
    "latency_s p50",
    "latency_s p95",
)


def config_row(c: ConfigSummary) -> list[str]:
    """One cell per CONFIG_COLUMNS entry; N is printed once, not inside each quantile."""
    return [
        c.config_id,
        str(c.task_success.N),
        c.task_success.fmt(),
        c.grader_pass.fmt(),
        c.subtask_pass.fmt(),
        f"{c.contention.n}/{c.contention.N} ({c.contention_events})",
        c.tokens_p50.fmt(0),
        c.tokens_p95.fmt(0),
        c.compute_s_p50.fmt(),
        c.compute_s_p95.fmt(),
        c.latency_s_p50.fmt(),
        c.latency_s_p95.fmt(),
    ]


def summarize(
    config: BenchConfig,
    records: Sequence[BenchRecord],
    provenance: Provenance,
    host: Sequence[HostSample] = (),
) -> BenchSummary:
    cells = [r for r in records if isinstance(r, CellResult)]
    by_config: dict[str, list[CellResult]] = defaultdict(list)
    for c in cells:
        by_config[c.config_id].append(c)
    adv = [r for r in records if isinstance(r, AdversaryResult)]
    pii = [r for r in records if isinstance(r, PiiCaseResult)]
    return BenchSummary(
        name=config.name,
        split=config.split,
        provenance=provenance,
        configs=tuple(summarize_config(s.id, by_config.get(s.id, [])) for s in config.configs),
        adversaries=summarize_adversaries(adv),
        adversary_rows=tuple(adv),
        pii_agents=summarize_pii(pii),
        pii_identical=pii_identical(pii),
        host=summarize_host(host),
        speedups=speedups(config.configs, cells),
    )


def format_summary(s: BenchSummary) -> list[str]:
    p = s.provenance
    lines = [
        f"bench {s.name} split={s.split} final={p.final} git={p.git_sha[:12]} "
        f"dirty={p.git_dirty} manifest={(p.manifest_sha256 or '-')[:12]} "
        f"generator={p.generator_version} grader={p.grader_version}",
        "model digests: "
        + (", ".join(f"{m}={d[:19]}" for m, d in sorted(p.model_digests.items())) or "-"),
        "ollama env: " + (", ".join(f"{k}={v}" for k, v in sorted(p.ollama_env.items())) or "-"),
        format_host(s.host),
    ]
    if s.configs:  # a PII-only or adversary-only config prints no empty config table
        lines += [
            "",
            "quantiles are value [95% order-statistic CI]; '-' bound = N too small for a CI",
            " | ".join(CONFIG_COLUMNS),
        ]
    for c in s.configs:
        lines.append(" | ".join(config_row(c)))
    for sp in s.speedups:
        ratio = "-" if sp.total_ratio is None else f"{sp.total_ratio:.2f}x"
        paired = "-" if sp.paired_ratio_p50 is None else f"{sp.paired_ratio_p50:.2f}x"
        lines.append(
            f"speedup {sp.serial_config} -> {sp.parallel_config}: paired cases "
            f"{sp.paired_cases}, wall {sp.wall_serial_s:.0f}s -> {sp.wall_parallel_s:.0f}s, "
            f"total {ratio}, per-case p50 {paired} (measured)"
        )
    if s.adversaries is not None:
        a = s.adversaries
        lines += [
            "",
            "id  mode  honest_real  status  actual  expected  exposure (real, canaries)  "
            "first_bad  detect_delay  strikes  harmed",
        ]
        for r in s.adversary_rows:
            lines.append(
                f"{r.id}  {r.mode}  {r.honest_real if r.honest_real is not None else 'default'}  "
                f"{r.status}  {r.actual}  {r.expected}  {r.exposure} ({r.exposure_real}, "
                f"{r.canaries})  {r.first_bad if r.first_bad is not None else '-'}  "
                f"{r.detect_delay if r.detect_delay is not None else '-'}  {r.strikes}  "
                f"{r.harmed}/{r.harmed_total}"
            )
        lines += [
            f"adversaries caught {a.caught.fmt()}",
            f"adversaries missed {a.missed.fmt()}",
            f"adversaries not-exposed {a.not_exposed.fmt()}",
            f"adversaries error {a.error.fmt()}",
            f"harmed tasks {a.harmed.fmt()}",
        ]
    if s.pii_agents:
        lines += [
            "",
            "pii separation (direct, no broker): agent | passed n/N [CI] | f1 p50 [CI] | "
            "completed n/N | s/case p50 | tokens/case p50 | tokens max | over task budget n/N | "
            "llm calls p50",
        ]
        for pa in s.pii_agents:
            lines.append(
                f"  {pa.agent_id} | {pa.passed.fmt()} | {pa.f1_p50.fmt(3)} | "
                f"{pa.completed.n}/{pa.completed.N} | "
                f"{pa.wall_s_p50.fmt() if pa.wall_s_p50 else '-'} | "
                f"{pa.tokens_p50.fmt(0) if pa.tokens_p50 else '-'} | "
                f"{'-' if pa.tokens_max is None else pa.tokens_max} | "
                f"{pa.over_budget.fmt() if pa.over_budget else '-'} | "
                f"{pa.calls_p50.fmt(0) if pa.calls_p50 else '-'}"
            )
        if s.pii_identical is not None:
            lines.append(
                f"pii outputs byte-identical across agents (output_sha256): "
                f"{s.pii_identical.fmt()} cases"
            )
    return lines


def sizing_lines(
    s: BenchSummary, *, hours: float = 8.0, candidates: Sequence[int] = (50, 100, 150, 200, 300)
) -> list[str]:
    """Seconds per case per config from a pilot -> N per config for `hours` of runs."""
    per_case = {
        c.config_id: c.wall_s_total / c.task_success.N for c in s.configs if c.task_success.N
    }
    adv_specs: dict[str, float] = {}
    for r in s.adversary_rows:
        adv_specs[f"{r.id}/{r.mode}"] = r.spec_wall_s
    lines = [f"sizing from pilot {s.name} (seconds per case = pilot total wall / pilot N)"]
    lines.append("config  pilot_N  s/case  " + "  ".join(f"N={n}:h" for n in candidates))
    for c in s.configs:
        if c.config_id not in per_case:
            lines.append(f"{c.config_id}  0  -  (no pilot cells)")
            continue
        spc = per_case[c.config_id]
        hrs = "  ".join(f"{n * spc / 3600:.2f}" for n in candidates)
        lines.append(f"{c.config_id}  {c.task_success.N}  {spc:.1f}  {hrs}")
    adv_s = sum(adv_specs.values())
    if adv_specs:
        lines.append(f"adversary specs {len(adv_specs)}: {adv_s:.0f}s total per suite pass")
    total_spc = sum(per_case.values())
    if total_spc > 0:
        budget = hours * 3600 - adv_s
        n_max = max(0, math.floor(budget / total_spc))
        lines.append(
            f"equal N per config within {hours:.1f} h (after one adversary pass): "
            f"N <= {n_max} ({n_max * total_spc / 3600:.2f} h cases + {adv_s / 3600:.2f} h adv)"
        )
    lines.append(
        "Wilson 95% half-width:  N  @p=0.5  " + "  ".join(f"@{c.config_id}" for c in s.configs)
    )
    for n in candidates:
        observed = []
        for c in s.configs:
            p = c.grader_pass.n / c.grader_pass.N if c.grader_pass.N else 0.5
            observed.append(f"{wilson_half_width(p, n):.3f}")
        lines.append(f"  {n}  {wilson_half_width(0.5, n):.3f}  " + "  ".join(observed))
    return lines
