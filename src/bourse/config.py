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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()
