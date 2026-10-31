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
    bourse_model_small: str = "gemma4:12b"
    bourse_admin_token: SecretStr = SecretStr("")
    # Comma-separated hosts accepted for any vendor's card URLs (dev only, e.g. localhost).
    bourse_dev_hosts: str = ""
    # Metering proxy: "agent_id:key,agent_id:key" (keys >= 16 chars). Never committed.
    bourse_meter_keys: SecretStr = SecretStr("")
    bourse_meter_max_tokens_per_call: int = 2048
    bourse_meter_deadline_s: float = 120.0
    bourse_meter_temperature: float = 0.0
    bourse_meter_seed: int = 1234
    bourse_meter_num_ctx: int = 8192
    bourse_meter_keep_alive: str = "30m"
    # Local stack wiring (bourse stack up sets these for its subprocesses).
    bourse_meter_url: str = "http://127.0.0.1:55200"
    bourse_registry_url: str = "http://127.0.0.1:55300"
    bourse_agent_key: SecretStr = SecretStr("")
    # Broker (Phase 3, ADR-0025): selection priors until Phase 4 measures them.
    bourse_broker_reliability_prior: float = 0.5
    bourse_broker_lambda_cost: float = 1.0
    bourse_broker_mu_latency: float = 0.005
    bourse_broker_shadow_prices: dict[str, float] = {
        "gemma4:12b": 0.10,
        "qwen3.5:27b-q4_K_M": 0.25,
    }
    bourse_broker_latency_priors: dict[str, float] = {
        "gemma4:12b": 5.0,
        "qwen3.5:27b-q4_K_M": 60.0,
    }
    bourse_broker_agent_models: dict[str, str] = {
        "vendor-a-profiler@vendor-a.local": "gemma4:12b",
        "vendor-b-pii-precise@vendor-b.local": "qwen3.5:27b-q4_K_M",
        "vendor-c-pii-fast@vendor-c.local": "gemma4:12b",
        "vendor-b-normalizer@vendor-b.local": "gemma4:12b",
        "vendor-a-reporter@vendor-a.local": "qwen3.5:27b-q4_K_M",
    }
    bourse_broker_delegation_deadline_s: float = 300.0
    bourse_broker_wall_budget_s: float = 1800.0
    bourse_broker_max_tokens_run: int = 20000

    @property
    def dev_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.bourse_dev_hosts.split(",") if h.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()
