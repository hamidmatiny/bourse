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
# Stack profiles (Phase 6b, ADR-0053): one stack-side control removed, the broker stays full B3.
StackProfile = Literal["full", "no-exam", "no-cardsig", "no-sinkhole", "no-metercap"]
STACK_PROFILES: tuple[str, ...] = ("full", "no-exam", "no-cardsig", "no-sinkhole", "no-metercap")
# The adversaries each removed control is meant to stop; a profile run runs exactly these.
PROFILE_ADVERSARIES: dict[str, tuple[str, ...]] = {
    "no-exam": ("A4a", "A7-raw", "A8a-raw", "A8b-raw"),
    "no-cardsig": ("A1", "A1b", "A2", "A3", "A9"),
    "no-sinkhole": ("A6a", "A6b", "A6c"),
    "no-metercap": ("A8a-raw", "A8a-patient", "A8b-raw", "A8b-patient", "A8c"),
}
# Mixed pool (ADR-0051 fix round 1): adversaries ACTIVE next to the honest agents.
MIX_POOL: tuple[str, ...] = ("A4b", "A5a", "A5b", "A6a", "A6b", "A6c", "A8c")
# Runtime adversaries a registration control (lint / exam) stops before any delivery, so they
# can never be exposed on that stack; their config spec must say so (ADR-0053 amendment 1).
_REGISTRATION_GATED = ("A4a", "A7-raw", "A8a-raw", "A8b-raw")
ADVERSARY_OUTCOMES: tuple[str, ...] = ("caught", "missed", "ineffective", "not-exposed", "error")


def registration_gated(adversary_id: str, profile: str) -> bool:
    """True when this adversary is rejected at registration on this stack (no-exam removes
    lint and exam, so there nothing gates it)."""
    return adversary_id in _REGISTRATION_GATED and profile != "no-exam"


Pool = Literal["clean", "mixed"]
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
    # mixed: MIX_POOL adversaries are re-admitted and ACTIVE for the whole config (6b).
    pool: Pool = "clean"

    @model_validator(mode="after")
    def _ablation_on_b3(self) -> "ConfigSpec":
        if self.ablation is not None and self.baseline != "B3":
            raise ValueError("ablations apply to B3 only")
        if self.pool == "mixed" and self.baseline == "B0":
            raise ValueError("B0 has no broker and no provider pool")
        return self


class AdversarySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    mode: Literal["mixed", "sole-provider", "card"] | None = None
    runs: int = Field(default=6, ge=1)
    honest_real: int | None = Field(default=None, ge=0)  # sleeper honest e2e tasks
    # Honest e2e runs before the scenario, all of which must succeed (a stack health gate; its
    # numbers are not part of the bench row). run_phase5 runs min(runs, baseline_runs).
    baseline_runs: int = Field(default=3, ge=1)
    # Wall cap for the whole spec (registration, exam, baseline, every mode). On expiry the
    # spec's row is an error with actual "spec wall cap <s>s" and the segment moves on.
    wall_cap_s: float = Field(default=3600.0, gt=0, le=4 * 3600)
    # Why this adversary cannot get a delivery on this stack (required exactly when it is
    # registration-gated; see registration_gated). A not-exposed row is never silent.
    no_exposure_reason: str | None = None


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
    # The stack this config must run on; preflight compares it with the live stack (ADR-0053).
    stack_profile: StackProfile = "full"

    @model_validator(mode="after")
    def _unique_ids(self) -> "BenchConfig":
        ids = [c.id for c in self.configs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate config id")
        return self

    @model_validator(mode="after")
    def _mixed_pool_runs_alone(self) -> "BenchConfig":
        """A mixed config needs a fresh stack (REVOKED is terminal; the adversary suite revokes
        vendor-x; another config would inherit quarantines): it is the only thing in its run."""
        mixed = [c for c in self.configs if c.pool == "mixed"]
        if mixed and (len(self.configs) > 1 or self.adversaries or self.pii_separation):
            raise ValueError(
                f"mixed config {mixed[0].id} must be the only config in its run, without "
                "adversaries or pii_separation (fresh stack per mixed config, ADR-0053)"
            )
        return self

    @model_validator(mode="after")
    def _profile_runs_only_its_adversaries(self) -> "BenchConfig":
        if self.stack_profile == "full":
            return self
        if self.configs or self.pii_separation is not None:
            raise ValueError(
                f"stack_profile {self.stack_profile}: adversaries only; the full-stack control "
                "rows come from the full-profile adversary suite (ADR-0053)"
            )
        want = set(PROFILE_ADVERSARIES[self.stack_profile])
        got = {a.id for a in self.adversaries}
        # dev split: a non-empty subset is allowed for short re-checks; test split: exactly.
        ok = (got and got <= want) if self.split == "dev" else got == want
        if not ok:
            rule = "a subset of" if self.split == "dev" else "exactly"
            raise ValueError(
                f"stack_profile {self.stack_profile} runs {rule} {sorted(want)}, got {sorted(got)}"
            )
        return self

    @model_validator(mode="after")
    def _exposure_reasons(self) -> "BenchConfig":
        for a in self.adversaries:
            gated = registration_gated(a.id, self.stack_profile)
            if gated and not a.no_exposure_reason:
                raise ValueError(
                    f"{a.id} is rejected at registration on stack {self.stack_profile}: "
                    "set no_exposure_reason (ADR-0053 amendment 1)"
                )
            if not gated and a.no_exposure_reason:
                raise ValueError(
                    f"{a.id} gets deliveries on stack {self.stack_profile} (sole-provider): "
                    "no_exposure_reason is only for registration-gated adversaries"
                )
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
    # Mixed pool (6b). Clean cells keep the defaults.
    pool: Pool = "clean"
    pool_epoch: str | None = None  # when this invocation re-admitted the pool (resume marker)
    delegations: int = Field(default=0, ge=0)  # A2A delegations in this cell (incl. retries)
    adversary_served: tuple[str, ...] = ()  # agent_id per delegation sent to an adversary
    harmed: bool | None = None  # None for clean cells
    harm_agents: tuple[str, ...] = ()  # adversaries that caused harm in this cell
    harm_detail: str = ""
    # Members that attempted their attack in this cell, harm or not (ADR-0053 amendment 1).
    attempt_agents: tuple[str, ...] = ()
    quarantined_adversaries: tuple[str, ...] = ()  # pool members QUARANTINED at cell end

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
    status: str  # one of ADVERSARY_OUTCOMES (bourse.adversaries.outcome)
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
    # ADR-0053 amendment 1 (defaults keep rows written before it readable).
    control: str = ""  # the control that stopped it (caught only)
    attempted: int = 0  # attack actions observed
    signals: str = ""  # controls that fired without stopping it
    exposure_target: int = 0  # k real deliveries aimed for (sole-provider); 0 = none
    rule: str = ""  # R1..R5
    note: str = ""  # under-exposure, infra detail, or the config's no_exposure_reason


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
    # Mixed pool only (6b): cases with harm, and subtasks served by an adversary.
    harmed: Proportion | None = None
    adversary_share: Proportion | None = None
    pool_adversaries: tuple["PoolAdversarySummary", ...] = ()


class AdversarySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    caught: Proportion
    missed: Proportion
    not_exposed: Proportion
    error: Proportion
    harmed: Proportion
    ineffective: Proportion | None = None  # None only in summaries from before amendment 1
    # Of the caught rows: stopped with harm 0 vs stopped after harm (detective controls, R2).
    # None only in summaries from before ADR-0049 amendment 2.
    caught_before_harm: Proportion | None = None
    caught_after_harm: Proportion | None = None


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


class PoolAdversarySummary(BaseModel):
    """One mixed-pool adversary over a config: subtasks served, harmful ones, and the cell
    (1-based, in run order) of its first harm and of its quarantine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    served: int
    harmed: int
    first_harm_cell: int | None
    quarantined_cell: int | None
    attempted: int = 0  # cells in which it attempted its attack (egress, misreport)

    @property
    def detect_delay_cells(self) -> int | None:
        if self.first_harm_cell is None or self.quarantined_cell is None:
            return None
        return self.quarantined_cell - self.first_harm_cell


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
    stack_profile: StackProfile = "full"  # live stack profile, checked against the config


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


ConfigSummary.model_rebuild()
