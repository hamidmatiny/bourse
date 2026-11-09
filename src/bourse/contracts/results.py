"""Tracked results for RESULTS.md (ADR-0054).

`results/<segment>/` holds the final summary.json, results.jsonl and provenance.json as the bench
wrote them, plus `evidence.json`: counts derived from the segment's database export. The export
is counts only. Violation evidence text, honeytokens and agent outputs are not copied.
"""

from pydantic import BaseModel, ConfigDict, Field


class ViolationCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    kind: str
    n: int = Field(ge=0)


class LedgerCount(BaseModel):
    """Ledger rows per (agent, skill, kind, state, spot_check_passed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    skill: str
    kind: str  # task | canary
    state: str
    spot_check_passed: bool | None
    n: int = Field(ge=0)


class SelectionCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str
    chosen: str | None
    reason: str
    n: int = Field(ge=0)


class SelectionTerms(BaseModel):
    """One candidate's terms in the last selection decision of a skill. B1 (random) and B2
    (prior) decisions carry no measured terms, so every term is optional."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    s: int | None
    f: int | None
    reliability_lb: float | None
    latency_ms: float | None
    cost_norm: float | None
    utility: float | None


class EvidenceDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment: str
    # False when the segment's database was gone before the export existed (final-suite).
    available: bool = True
    ledger_rows: int = Field(ge=0)
    violations: tuple[ViolationCount, ...]
    ledger: tuple[LedgerCount, ...]
    selections: tuple[SelectionCount, ...]
    last_pii_selection: tuple[SelectionTerms, ...]


class ScenarioLine(BaseModel):
    """One adversary scenario line from a Phase 5c acceptance log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adversary: str
    mode: str
    status: str
    exposure: int
    exposure_real: int
    canaries: int
    first_bad: int | None
    detect_delay: int | None
    strikes: int
    rel_epoch: int


class Phase5cEvidence(BaseModel):
    """Parsed from data/logs/acceptance5c/*.log. Approver user ids are not kept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catch: ScenarioLine
    recatch: ScenarioLine
    approve_exam: str  # "5/5"
    approve_to_state: str
    approve_epoch_from: int
    approve_epoch_to: int
    deny_exam: str
    deny_to_state: str
    slack_approve: str  # "approved=True reason=operator approved"
    slack_timeout: str  # "approved=False reason=timeout"


class HostHardware(BaseModel):
    """The machine the final blocks ran on, recorded by `bench results-export` on that machine.
    host.jsonl records power and load per run, not the hardware, so this is read at export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None
    cpu: str | None
    cpu_count: int | None
    memory_bytes: int | None
    os: str
    ollama_version: str | None
    docker_version: str | None
