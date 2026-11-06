"""The five honest agents (ADR-0021 packaging; ADR-0047 single-model roster).

Roster models below are the single source of truth (`ROSTER_MODELS`). Packaging
(vendors, frameworks, ports) remains ADR-0021; models are ADR-0047
(all `qwen3.5:27b-q4_K_M`). `BOURSE_MODEL_SMALL` / `BOURSE_MODEL_LARGE` and broker
agent→model maps must match; a unit test fails on drift. Any future model change
needs an ADR with measured memory + exam numbers and operator approval.
"""

import httpx
import uvicorn
from fastapi import FastAPI

from bourse.agents.common.app import build_agent_app
from bourse.agents.common.card import latest_vendor_key, signed_card
from bourse.agents.common.executor import SkillExecutor
from bourse.agents.common.spec import AgentRuntime, AgentSpec
from bourse.agents.normalizer.agent import NormalizerExecutor
from bourse.agents.pii_fast.agent import PiiFastExecutor
from bourse.agents.pii_precise.agent import PiiPreciseExecutor
from bourse.agents.profiler.agent import ProfilerExecutor
from bourse.agents.reporter.agent import ReporterExecutor
from bourse.config import Settings
from bourse.contracts.skills import SkillId
from bourse.mcp_servers.common import SERVER_PORTS
from bourse.telemetry import setup_tracing, traced_async_client

AGENT_NAMES = ("profiler", "pii_precise", "pii_fast", "normalizer", "reporter")

# ADR-0047 single-model roster (supersedes ADR-0021 model column).
ROSTER_MODEL = "qwen3.5:27b-q4_K_M"
ROSTER_SMALL_MODEL = ROSTER_MODEL  # env knob alias; equal to large under ADR-0047
ROSTER_LARGE_MODEL = ROSTER_MODEL
ROSTER_MODELS: dict[str, str] = {
    "profiler": ROSTER_MODEL,
    "pii_precise": ROSTER_MODEL,
    "pii_fast": ROSTER_MODEL,
    "normalizer": ROSTER_MODEL,
    "reporter": ROSTER_MODEL,
}
TABLE_TOOLS = frozenset({"profile_columns", "column_stats", "sample_rows"})
PII_TOOLS = frozenset({"regex_scan", "luhn_check", "sin_check"})
UNITS_TOOLS = frozenset({"parse_header_unit", "convert"})

EXECUTORS: dict[str, type[SkillExecutor]] = {
    "profiler": ProfilerExecutor,
    "pii_precise": PiiPreciseExecutor,
    "pii_fast": PiiFastExecutor,
    "normalizer": NormalizerExecutor,
    "reporter": ReporterExecutor,
}


def agent_specs(large_model: str, small_model: str) -> dict[str, AgentSpec]:
    return {
        "profiler": AgentSpec(
            name="profiler",
            framework="langgraph",
            vendor_domain="vendor-a.local",
            vendor_org="Vendor A",
            display_name="Vendor A Profiler",
            description="Schema profile and column statistics from deterministic table tools.",
            version="1.0.0",
            skills=(SkillId.SCHEMA_PROFILE, SkillId.STATS_COMPUTE),
            model=small_model,
            port=55401,
            mcp={"table": TABLE_TOOLS},
        ),
        "pii_precise": AgentSpec(
            name="pii_precise",
            framework="crewai",
            vendor_domain="vendor-b.local",
            vendor_org="Vendor B",
            display_name="Vendor B PII Precise",
            description=(
                "Cell-level PII detection; names, contact columns and contacts in free text "
                "proposed by an LLM and accepted by deterministic checks."
            ),
            version="1.1.0",  # ADR-0052: contact_check step; re-signed, re-examined
            skills=(SkillId.PII_DETECT,),
            model=large_model,
            port=55402,
            mcp={"pii": PII_TOOLS | {"contact_check"}, "table": frozenset({"sample_rows"})},
        ),
        "pii_fast": AgentSpec(
            name="pii_fast",
            framework="a2a-sdk",
            vendor_domain="vendor-c.local",
            vendor_org="Vendor C",
            display_name="Vendor C PII Fast",
            description="Cell-level PII detection with one short LLM call for names.",
            version="1.0.0",
            skills=(SkillId.PII_DETECT,),
            model=small_model,
            port=55403,
            mcp={"pii": PII_TOOLS},
        ),
        "normalizer": AgentSpec(
            name="normalizer",
            framework="a2a-sdk",
            vendor_domain="vendor-b.local",
            vendor_org="Vendor B",
            display_name="Vendor B Normalizer",
            description="Unit detection and conversion factors to canonical units.",
            version="1.0.0",
            skills=(SkillId.UNITS_NORMALIZE,),
            model=small_model,
            port=55404,
            mcp={"units": UNITS_TOOLS},
        ),
        "reporter": AgentSpec(
            name="reporter",
            framework="langgraph",
            vendor_domain="vendor-a.local",
            vendor_org="Vendor A",
            display_name="Vendor A Reporter",
            description="Short report whose cited numbers are checked against its input.",
            version="1.0.0",
            skills=(SkillId.REPORT_COMPOSE,),
            model=large_model,
            port=55405,
            mcp={},
        ),
    }


def specs_from_settings(settings: Settings) -> dict[str, AgentSpec]:
    return agent_specs(settings.bourse_model_large, settings.bourse_model_small)


def mcp_urls(template: str = "http://127.0.0.1:{port}/mcp") -> dict[str, str]:
    """BOURSE_MCP_URL_TEMPLATE: host mode uses 127.0.0.1, compose uses mcp-<name> (ADR-0034)."""
    return {name: template.format(name=name, port=port) for name, port in SERVER_PORTS.items()}


def build_app(spec: AgentSpec, runtime: AgentRuntime, base_url: str) -> FastAPI:
    executor = EXECUTORS[spec.name](spec, runtime)
    card = signed_card(spec, base_url, latest_vendor_key(spec.vendor_domain))
    return build_agent_app(executor, card)


def serve_agent(name: str, settings: Settings, meter_url: str, meter_key: str) -> None:
    import os
    from dataclasses import replace

    spec = specs_from_settings(settings)[name]
    # Throwaway probes may bump the card version without changing the committed roster.
    card_version = os.environ.get("BOURSE_CARD_VERSION", "").strip()
    if card_version:
        spec = replace(spec, version=card_version)
    if name == "pii_precise":
        from bourse.agents.pii_precise import isolation

        isolation.apply()
    setup_tracing(f"bourse-agent-{name}", settings.otel_exporter_otlp_endpoint)
    client = traced_async_client(
        timeout=httpx.Timeout(settings.bourse_agent_llm_timeout_s, connect=5.0)
    )
    runtime = AgentRuntime(
        meter_url=meter_url,
        meter_key=meter_key,
        http_client=client,
        mcp_servers=mcp_urls(settings.bourse_mcp_url_template),
    )
    # Compose: the card names the vendor hostname (e.g. profiler.vendor-a.local).
    base_url = settings.bourse_agent_base_url or f"http://127.0.0.1:{spec.port}"
    uvicorn.run(build_app(spec, runtime, base_url), host=settings.bourse_bind_host, port=spec.port)
