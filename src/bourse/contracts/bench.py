"""BOURSE-Bench contracts (ADR-0051): cases, manifests, configs, per-cell results, summaries."""

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Split = Literal["dev", "test"]
Domain = Literal["people", "orders", "measurements", "clinic", "shipments", "hard_pii"]
Baseline = Literal["B0", "B1", "B2", "B3"]
Ablation = Literal[
    "no-spotcheck", "no-canary", "no-metercap", "no-exam", "no-cardsig", "no-sinkhole"
]
# Ablations the broker can apply itself; the rest need a stack variant (ADR-0051).
BROKER_ABLATIONS: frozenset[str] = frozenset({"no-spotcheck", "no-canary"})
SHA256 = r"^[0-9a-f]{64}$"


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class BenchCase(BaseModel):
    """One generated dataset-intake job with its deterministic truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    split: Split
    domain: Domain
    seed: str
    request_text: str
    csv_text: str
    truth: dict[str, Any]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    domain: Domain
    seed: str
    sha256: str = Field(pattern=SHA256)


class BenchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    split: Split
    generator_version: str
    grader_version: str
    base_seed: str
    cases: tuple[ManifestEntry, ...]

    @model_validator(mode="after")
    def _unique(self) -> "BenchManifest":
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case_id in manifest")
        return self


class ConfigSpec(BaseModel):
    """One benchmark configuration: a baseline, optionally with one B3 ablation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    baseline: Baseline
    ablation: Ablation | None = None
    subtask_slots: int = Field(default=2, ge=1, le=8)
    repeats: int = Field(default=1, ge=1)
    # First N of the run's cases for this config (B0 N=50 vs 100, ADR-0051); None = all.
    cases: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ablation_on_b3(self) -> "ConfigSpec":
        if self.ablation is not None and self.baseline != "B3":
            raise ValueError("ablations apply to B3 only")
        return self


class AdversarySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    mode: Literal["mixed", "sole-provider", "card"] | None = None
    runs: int = Field(default=6, ge=1)
    honest_real: int | None = Field(default=None, ge=0)  # sleeper honest e2e tasks


class PiiSeparationSpec(BaseModel):
    """Send the same dev cases straight to each PII agent (no broker) and grade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agents: tuple[str, ...] = Field(min_length=1)
    domains: tuple[Domain, ...] = ("hard_pii",)
    cases: int = Field(default=5, ge=1)


class BenchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    split: Split = "dev"
    cases: int | None = Field(default=None, ge=1)  # first N cases of the split; None = all
    domains: tuple[Domain, ...] | None = None  # filter; None = all
    configs: tuple[ConfigSpec, ...] = ()
    adversaries: tuple[AdversarySpec, ...] = ()
    pii_separation: PiiSeparationSpec | None = None
    model_digests: dict[str, str] = Field(default_factory=dict)  # model tag -> sha256 digest

    @model_validator(mode="after")
    def _unique_ids(self) -> "BenchConfig":
        ids = [c.id for c in self.configs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate config id")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()


class SubtaskGrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str
    skill: str
    status: str
    agent_id: str | None
    passed: bool
    n: int
    N: int
    detail: str = ""


class CellResult(BaseModel):
    """One (config, case, repeat) cell. Written to results.jsonl when the cell finishes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Literal["cell"] = "cell"
    key: str
    config_id: str
    case_id: str
    domain: Domain
    repeat: int
    run_id: str
    status: str  # SUCCEEDED | FAILED | ABORTED | ERROR
    planner_used: str
    subtasks: tuple[SubtaskGrade, ...]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    compute_ms: float = Field(ge=0)  # sum of Ollama total_duration_ms (fallback wall_ms)
    wall_s: float = Field(ge=0)
    contention_events: int = Field(ge=0)
    error: str | None = None
    started_at: datetime
    finished_at: datetime

    @property
    def all_passed(self) -> bool:
        return bool(self.subtasks) and all(s.passed for s in self.subtasks)


class AdversaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Literal["adversary"] = "adversary"
    key: str
    id: str
    mode: str
    honest_real: int | None
    status: str  # caught | missed | not-exposed | error | zero-effect | ...
    actual: str
    expected: str
    exposure: int
    exposure_real: int
    canaries: int
    first_bad: int | None
    detect_delay: int | None
    strikes: int
    harmed: int
    harmed_total: int
    spec_wall_s: float = Field(ge=0)  # wall of the whole adversary spec invocation
    finished_at: datetime


class PiiCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Literal["pii"] = "pii"
    key: str
    agent_id: str
    case_id: str
    state: str
    passed: bool
    f1: float
    tp: int
    N: int
    detail: str
    wall_s: float
    finished_at: datetime
    # sha256 of canonical_json(output); None for records written before 6a fix round 1.
    output_sha256: str | None = None
    # Meter rows for this task id (ADR-0052 budget check); None before fix round 2.
    tokens_in: int | None = None
    tokens_out: int | None = None
    llm_calls: int | None = None
    task_max_tokens: int | None = None  # the per-task budget the task ran under


BenchRecord = CellResult | AdversaryResult | PiiCaseResult


class Proportion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    n: int = Field(ge=0)
    N: int = Field(ge=0)
    ci_low: float
    ci_high: float

    def fmt(self) -> str:
        return f"{self.n}/{self.N} [{self.ci_low:.2f}, {self.ci_high:.2f}]"


class Quantile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    q: float
    value: float | None
    ci_low: float | None
    ci_high: float | None
    N: int

    def fmt(self, digits: int = 1, *, with_n: bool = False) -> str:
        """`value [lo, hi]`; a bound is `-` when N is too small for an order-statistic CI.
        N is appended only on request (tables print it once per row)."""
        if self.value is None:
            return "-"
        lo = "-" if self.ci_low is None else f"{self.ci_low:.{digits}f}"
        hi = "-" if self.ci_high is None else f"{self.ci_high:.{digits}f}"
        out = f"{self.value:.{digits}f} [{lo}, {hi}]"
        return f"{out} (N={self.N})" if with_n else out


class ConfigSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_id: str
    task_success: Proportion
    grader_pass: Proportion
    subtask_pass: Proportion
    contention: Proportion
    contention_events: int
    tokens_p50: Quantile
    tokens_p95: Quantile
    compute_s_p50: Quantile
    compute_s_p95: Quantile
    latency_s_p50: Quantile
    latency_s_p95: Quantile
    wall_s_total: float


class AdversarySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    caught: Proportion
    missed: Proportion
    not_exposed: Proportion
    error: Proportion
    harmed: Proportion


class PiiAgentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    passed: Proportion
    f1_p50: Quantile
    completed: Proportion
    tokens_p50: Quantile | None = None  # tokens_in + tokens_out per case
    tokens_max: int | None = None
    calls_p50: Quantile | None = None
    wall_s_p50: Quantile | None = None
    over_budget: Proportion | None = None  # cases with tokens > task_max_tokens


class Speedup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    serial_config: str
    parallel_config: str
    paired_cases: int
    wall_serial_s: float
    wall_parallel_s: float
    total_ratio: float | None
    paired_ratio_p50: float | None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final: bool
    git_sha: str
    git_dirty: bool
    manifest_sha256: str | None
    model_digests: dict[str, str]
    ollama_env: dict[str, str]
    generator_version: str
    grader_version: str
    config_sha256: str
    at: datetime


class HostSample(BaseModel):
    """Host conditions at one moment of a bench run (macOS pmset + load average), written by
    `bourse bench launch` on the host into data/bench/<name>/host.jsonl (ADR-0051)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    phase: Literal["start", "during", "end"]
    power_source: str | None  # "AC Power" / "Battery Power" (pmset -g batt)
    battery_pct: int | None
    battery_state: str | None  # charging / discharging / charged / AC attached
    powermode: int | None  # pmset -g "Currently in use" powermode: 0 auto, 1 low, 2 high
    low_power_mode: bool | None  # powermode == 1
    thermal_warning: bool | None  # pmset -g therm reports a thermal/performance warning
    cpu_speed_limit: int | None  # pmset -g therm CPU_Speed_Limit (%), when reported
    load_1m: float
    load_5m: float
    load_15m: float
    error: str | None = None  # pmset unavailable or failed; load averages still recorded


class HostSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: int
    on_battery: Proportion  # samples with power_source "Battery Power"
    low_power: Proportion  # samples with low_power_mode true
    thermal_warning: Proportion
    battery_min_pct: int | None
    cpu_speed_limit_min: int | None
    load_1m_p50: float | None
    load_1m_max: float | None


class BenchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    split: Split
    provenance: Provenance
    configs: tuple[ConfigSummary, ...]
    adversaries: AdversarySummary | None
    adversary_rows: tuple[AdversaryResult, ...]
    pii_agents: tuple[PiiAgentSummary, ...]
    speedups: tuple[Speedup, ...]
    # Cases where every PII agent's output_sha256 is equal, over cases with a hash from all.
    pii_identical: Proportion | None = None
    # Host conditions from host.jsonl; None when the run was not started by `bench launch`.
    host: HostSummary | None = None
