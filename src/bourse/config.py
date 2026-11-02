"""Settings from environment; `.env` is loaded with python-dotenv (never committed)."""

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_num_parallel: int = 1
    omniroute_base_url: str = "http://127.0.0.1:20128"
    omniroute_api_key: SecretStr = SecretStr("")
    omniroute_model: str = ""
    bourse_database_url: str = "postgresql+psycopg://bourse:bourse@127.0.0.1:55432/bourse"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:54317"
    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    slack_channel_id: str = ""
    bourse_model_large: str = "qwen3.5:27b-q4_K_M"
    bourse_model_small: str = "qwen3.5:4b"
    bourse_admin_token: SecretStr = SecretStr("")
    # Comma-separated hosts accepted for any vendor's card URLs (dev only, e.g. localhost).
    bourse_dev_hosts: str = ""
    # Metering proxy: "agent_id:key,agent_id:key" (keys >= 16 chars). Never committed.
    bourse_meter_keys: SecretStr = SecretStr("")
    bourse_meter_max_tokens_per_call: int = 2048
    bourse_meter_deadline_s: float = 120.0
    bourse_meter_temperature: float = 0.0
    bourse_meter_seed: int = 1234
    # Single source for options.num_ctx on every Ollama request (ADR-0038): "model=n,...";
    # "*" is used for models not listed. Rule: smallest of 4096/6144/8192 >= 2 x observed max.
    bourse_num_ctx_by_model: str = (
        "qwen3.5:4b=4096,gemma4:e4b=4096,gemma4:12b=4096,qwen3.5:27b-q4_K_M=4096,*=4096"
    )
    bourse_meter_keep_alive: str = "30m"
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
    bourse_broker_agent_models: dict[str, str] = {
        "vendor-a-profiler@vendor-a.local": "qwen3.5:4b",
        "vendor-b-pii-precise@vendor-b.local": "qwen3.5:27b-q4_K_M",
        "vendor-c-pii-fast@vendor-c.local": "qwen3.5:4b",
        "vendor-b-normalizer@vendor-b.local": "qwen3.5:4b",
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
    def dev_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.bourse_dev_hosts.split(",") if h.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()
