"""Settings from environment; `.env` is loaded with python-dotenv (never committed)."""

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    ollama_base_url: str = "http://127.0.0.1:11434"
    # Must match host OLLAMA_NUM_PARALLEL; meter global gate size (ADR-0044).
    ollama_num_parallel: int = 2
    omniroute_base_url: str = "http://127.0.0.1:20128"
    omniroute_api_key: SecretStr = SecretStr("")
    omniroute_model: str = ""
    bourse_database_url: str = "postgresql+psycopg://bourse:bourse@127.0.0.1:55432/bourse"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:54317"
    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    slack_channel_id: str = ""
    # Comma-separated Slack user IDs allowed to Approve/Deny (ADR-0048).
    bourse_slack_approvers: str = ""
    # Default approval wait (seconds); timeout -> deny reason "timeout".
    bourse_slack_timeout_s: float = 900.0
    # Compose approver service (ADR-0048). Internal callers use URL+key; no Slack tokens.
    bourse_approver_url: str = ""
    bourse_approver_key: SecretStr = SecretStr("")
    # Set to 1 inside compose containers (host CLI uses this for refuse-if-compose).
    bourse_in_compose: str = ""
    # Stack profile (ADR-0053): one stack-side control removed for an ablation. Set by
    # `bourse stack up --profile` in every service's env file; "full" everywhere else.
    bourse_stack_profile: Literal["full", "no-exam", "no-cardsig", "no-sinkhole", "no-metercap"] = (
        "full"
    )
    # ADR-0047: both knobs equal the single roster model (aliases for compose/env).
    bourse_model_large: str = "qwen3.5:27b-q4_K_M"
    bourse_model_small: str = "qwen3.5:27b-q4_K_M"
    bourse_admin_token: SecretStr = SecretStr("")
    # Comma-separated hosts accepted for any vendor's card URLs (dev only, e.g. localhost).
    bourse_dev_hosts: str = ""
    # Metering proxy: "agent_id:key,agent_id:key" (keys >= 16 chars). Never committed.
    bourse_meter_keys: SecretStr = SecretStr("")
    bourse_meter_max_tokens_per_call: int = 2048
    bourse_meter_deadline_s: float = 120.0
    # Agent→meter HTTP timeout per LLM call (ADR-0047). Must be >= meter_deadline_s.
    bourse_agent_llm_timeout_s: float = 180.0
    bourse_meter_temperature: float = 0.0
    bourse_meter_seed: int = 1234
    # Single source for options.num_ctx on every Ollama request (ADR-0038): "model=n,...";
    # "*" is used for models not listed. Rule: smallest of 4096/6144/8192 >= 2 x observed max.
    bourse_num_ctx_by_model: str = "qwen3.5:27b-q4_K_M=4096,*=4096"
    # Single keep_alive for meter and probe Ollama calls (ADR-0044). Default 30m.
    bourse_meter_keep_alive: str = "30m"
    # Trust engine (ADR-0039).
    bourse_trust_strikes: int = 3
    bourse_trust_window_s: float = 86400.0
    # Per-task meter budgets (ADR-0041).
    bourse_task_max_tokens: int = 8192
    bourse_task_max_calls: int = 20
    # Meter fair share (ADR-0042 r2): max concurrent upstream calls and token pace per agent.
    bourse_meter_max_in_flight: int = 1
    bourse_meter_tokens_per_min: float = 60_000.0
    # Worst-case prompt tokens for bucket-capacity check (must fit in tokens_per_min).
    bourse_meter_max_prompt_tokens: int = 4096
    # Gate queue wait hard cap; must be < bourse_meter_deadline_s (CONTENTION on timeout).
    bourse_meter_queue_timeout_s: float = 60.0
    # CONTENTION when queue_wait_ms > this fraction of the delegation deadline.
    bourse_contention_queue_fraction: float = 0.5
    # Registration rate limits (ADR-0043): token bucket per IP and per vendor.
    bourse_register_rate_per_min: int = 10
    # Unauthenticated inline-card violations stored individually before aggregation.
    bourse_inline_violations_per_hour: int = 20
    # Local stack wiring (bourse stack up sets these for its subprocesses).
    bourse_meter_url: str = "http://127.0.0.1:55200"
    bourse_registry_url: str = "http://127.0.0.1:55300"
    bourse_agent_key: SecretStr = SecretStr("")
    # Bind address of served apps (127.0.0.1 on the host; 0.0.0.0 inside compose containers).
    bourse_bind_host: str = "127.0.0.1"
    # Agent card base URL; empty -> http://127.0.0.1:<port>. Compose: vendor hostnames.
    bourse_agent_base_url: str = ""
    # MCP server URL template for agents ({name}, {port}).
    bourse_mcp_url_template: str = "http://127.0.0.1:{port}/mcp"
    # Where the resident monitor reads Ollama /api/ps (compose: via the meter).
    bourse_ollama_ps_url: str = "http://127.0.0.1:11434/api/ps"
    bourse_ollama_ps_key: SecretStr = SecretStr("")
    # Admission exam and canaries (ADR-0037).
    bourse_exam_k: int = 5
    bourse_exam_pass: int = 4
    bourse_exam_cooldown_s: float = 600.0
    bourse_exam_deadline_s: float = 300.0
    # Per-model exam task deadlines from probe --latency: "model=seconds,..." =
    # max(base, 3*p95_total_s). Empty -> base only (ADR-0044).
    bourse_exam_deadline_by_model: str = ""
    # Whole-exam wall clock; timed-out tasks count as EXAM_TIMEOUT failures.
    bourse_exam_timeout_s: float = 900.0
    bourse_canary_every: int = 10
    bourse_honeytoken_secret: SecretStr = SecretStr("")
    # Egress sinkhole (ADR-0035).
    bourse_sinkhole_ports: str = "80,443"
    # Broker (Phase 3, ADR-0025): selection priors until Phase 4 measures them.
    bourse_broker_reliability_prior: float = 0.5
    bourse_broker_lambda_cost: float = 1.0
    bourse_broker_mu_latency: float = 0.005
    bourse_broker_shadow_prices: dict[str, float] = {
        "qwen3.5:4b": 0.04,
        "gemma4:e4b": 0.05,
        "gemma4:12b": 0.10,
        "qwen3.5:27b-q4_K_M": 0.25,
    }
    bourse_broker_latency_priors: dict[str, float] = {
        "qwen3.5:4b": 3.0,
        "gemma4:e4b": 3.0,
        "gemma4:12b": 5.0,
        "qwen3.5:27b-q4_K_M": 60.0,
    }
    # ADR-0047: all honest agents on the single roster model.
    bourse_broker_agent_models: dict[str, str] = {
        "vendor-a-profiler@vendor-a.local": "qwen3.5:27b-q4_K_M",
        "vendor-b-pii-precise@vendor-b.local": "qwen3.5:27b-q4_K_M",
        "vendor-c-pii-fast@vendor-c.local": "qwen3.5:27b-q4_K_M",
        "vendor-b-normalizer@vendor-b.local": "qwen3.5:27b-q4_K_M",
        "vendor-a-reporter@vendor-a.local": "qwen3.5:27b-q4_K_M",
    }
    bourse_broker_delegation_deadline_s: float = 300.0
    bourse_broker_wall_budget_s: float = 1800.0
    bourse_broker_max_tokens_run: int = 20000
    # Ledger and measured selection (Phase 4, ADR-0029..0031).
    bourse_prices_file: str = "config/prices.toml"
    bourse_select_lambda: float = 0.2
    bourse_select_mu: float = 0.2
    bourse_select_ewma_alpha: float = 0.3
    bourse_select_min_trials: int = 5
    bourse_select_explore_every: int = 4
    bourse_select_cold_tokens: int = 2000
    # Resident memory guard (ADR-0033).
    bourse_resident_limit_bytes: int = 26_000_000_000
    bourse_resident_sustain_s: float = 30.0
    bourse_resident_interval_s: float = 5.0

    @model_validator(mode="after")
    def _timeout_ordering(self) -> "Settings":
        """ADR-0047: agent LLM timeout >= meter deadline < exam task deadline <= exam_timeout_s."""
        agent = float(self.bourse_agent_llm_timeout_s)
        meter = float(self.bourse_meter_deadline_s)
        exam_task = float(self.bourse_exam_deadline_s)
        exam_wall = float(self.bourse_exam_timeout_s)
        if not (agent >= meter < exam_task <= exam_wall):
            raise ValueError(
                "timeout ordering violated (ADR-0047): require "
                "bourse_agent_llm_timeout_s >= bourse_meter_deadline_s "
                "< bourse_exam_deadline_s <= bourse_exam_timeout_s; "
                f"got agent={agent} meter={meter} exam_task={exam_task} exam_wall={exam_wall}"
            )
        return self

    @property
    def num_ctx_by_model(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in (e.strip() for e in self.bourse_num_ctx_by_model.split(",")):
            if entry:
                model, _, value = entry.rpartition("=")
                out[model.strip()] = int(value)
        return out

    def num_ctx_for(self, model: str) -> int:
        table = self.num_ctx_by_model
        if model in table:
            return table[model]
        if "*" in table:
            return table["*"]
        raise KeyError(f"no num_ctx for model {model!r} in BOURSE_NUM_CTX_BY_MODEL")

    @property
    def exam_deadline_by_model(self) -> dict[str, float]:
        """model -> task deadline seconds (from BOURSE_EXAM_DEADLINE_BY_MODEL or empty)."""
        out: dict[str, float] = {}
        for entry in (e.strip() for e in self.bourse_exam_deadline_by_model.split(",")):
            if not entry:
                continue
            model, _, value = entry.rpartition("=")
            if model and value:
                out[model.strip()] = float(value)
        return out

    @property
    def roster_models(self) -> frozenset[str]:
        """Distinct model tags the honest roster may load (ADR-0047)."""
        return frozenset({self.bourse_model_small, self.bourse_model_large})

    @property
    def dev_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.bourse_dev_hosts.split(",") if h.strip())

    @property
    def slack_approver_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.bourse_slack_approvers.split(",") if x.strip())

    @property
    def slack_env_complete(self) -> bool:
        """True when SlackApprover can run (ADR-0048)."""
        return bool(
            self.slack_bot_token.get_secret_value()
            and self.slack_app_token.get_secret_value()
            and self.slack_channel_id.strip()
            and self.slack_approver_ids
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()
