"""Serve one adversary A2A agent, or the static Phase-1 attack card host."""

from typing import Any

import httpx
import uvicorn
from a2a.types import AgentCard
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bourse.adversaries.behaviors import AdversaryExecutor
from bourse.adversaries.catalog import (
    BY_NAME,
    CARD_ATTACKS_NAME,
    VENDOR,
    adversary_specs,
)
from bourse.agents.catalog import mcp_urls
from bourse.agents.common.app import build_agent_app
from bourse.agents.common.card import latest_vendor_key, vendor_jwks_url
from bourse.agents.common.spec import AgentRuntime
from bourse.config import Settings
from bourse.crypto.card_sign import sign_card
from bourse.crypto.jcs import card_to_dict
from bourse.crypto.keys import generate_key, thumbprint_kid
from bourse.telemetry import setup_tracing, traced_async_client


def serve_adversary(name: str, settings: Settings, meter_url: str, meter_key: str) -> None:
    if name == CARD_ATTACKS_NAME:
        serve_card_attacks(settings)
        return
    adv = BY_NAME[name]
    spec = adversary_specs(settings.bourse_model_small)[name]
    setup_tracing(f"bourse-adversary-{name}", settings.otel_exporter_otlp_endpoint)
    client = traced_async_client(
        timeout=httpx.Timeout(settings.bourse_agent_llm_timeout_s, connect=5.0)
    )
    runtime = AgentRuntime(
        meter_url=meter_url,
        meter_key=meter_key,
        http_client=client,
        mcp_servers=mcp_urls(settings.bourse_mcp_url_template),
    )
    base_url = settings.bourse_agent_base_url or f"http://127.0.0.1:{spec.port}"
    skill_desc = {spec.skills[0]: adv.skill_description} if adv.skill_description else None
    from bourse.agents.common.card import build_card

    key = latest_vendor_key(spec.vendor_domain)
    card = sign_card(
        build_card(spec, base_url, skill_desc),
        key,
        thumbprint_kid(key),
        vendor_jwks_url(spec.vendor_domain),
    )
    executor = AdversaryExecutor(spec, runtime, adv.behavior)
    app = build_agent_app(executor, card)

    @app.post("/bourse/reset-behavior")
    def reset_behavior() -> dict[str, object]:
        """Clear sleeper prefix counter + behavior log before admission exam (ADR-0042).

        Exam tasks count toward EXAM_TASKS+HONEST_REAL; reset sets both to a clean prefix.
        """
        executor.reset_behavior()
        return {"ok": True, "completed": executor._completed, "log_len": 0}

    @app.get("/bourse/behavior-log")
    def behavior_log() -> dict[str, object]:
        """Per-execute awake/deviated events for phase5 first_bad indexing."""
        return {"events": executor.behavior_log()}

    uvicorn.run(app, host=settings.bourse_bind_host, port=spec.port)


def _attack_cards(settings: Settings) -> dict[str, dict[str, Any]]:
    """Signed attack cards for A1-A3, A9. Each Host alias has its own agent identity."""
    from bourse.agents.common.card import build_card
    from bourse.agents.common.spec import AgentSpec
    from bourse.crypto.jcs import card_from_dict

    base_spec = adversary_specs(settings.bourse_model_small)["poison-quiet"]
    vendor_key = latest_vendor_key(VENDOR)
    kid = thumbprint_kid(vendor_key)
    jku = vendor_jwks_url(VENDOR)

    def _spec(name: str, display: str) -> AgentSpec:
        return AgentSpec(
            name=name,
            framework=base_spec.framework,
            vendor_domain=VENDOR,
            vendor_org=base_spec.vendor_org,
            display_name=display,
            description=base_spec.description,
            version="1.0.0",
            skills=base_spec.skills,
            model=base_spec.model,
            port=55520,
            mcp=base_spec.mcp,
        )

    def _signed(
        host: str, display: str, *, version: str = "1.0.0", url: str | None = None
    ) -> AgentCard:
        origin = url or f"http://{host}.{VENDOR}:55520"
        card = build_card(_spec(host, display), origin)
        if version != "1.0.0":
            data = card_to_dict(sign_card(card, vendor_key, kid, jku))
            unsigned = {k: v for k, v in data.items() if k != "signatures"}
            return sign_card(card_from_dict({**unsigned, "version": version}), vendor_key, kid, jku)
        return sign_card(card, vendor_key, kid, jku)

    control = _signed("control", "Card Attacks Control")
    control_dict = card_to_dict(control)
    attacker = generate_key()
    a1_base = build_card(_spec("a1", "A1 Forged"), f"http://a1.{VENDOR}:55520")
    a1 = sign_card(a1_base, attacker, thumbprint_kid(attacker), jku)
    a1b = _signed("a1b", "A1b Domain", url="http://profiler.vendor-a.local:55401")
    a2_signed = _signed("a2", "A2 Tampered")
    a2 = dict(card_to_dict(a2_signed))
    a2["description"] = (a2.get("description") or "") + " tampered"
    a3_base = build_card(_spec("a3", "A3 Untrusted Jku"), f"http://a3.{VENDOR}:55520")
    a3 = sign_card(a3_base, vendor_key, kid, "https://attacker.example/jwks")
    v12 = _signed("a9-1.2.0", "A9 Version", version="1.2.0")
    v11 = sign_card(
        card_from_dict(
            {
                **{k: v for k, v in card_to_dict(v12).items() if k != "signatures"},
                "version": "1.1.0",
                "name": "A9 Version",
            }
        ),
        vendor_key,
        kid,
        jku,
    )
    reuse = sign_card(
        card_from_dict(
            {
                **{k: v for k, v in card_to_dict(v12).items() if k != "signatures"},
                "version": "1.2.0",
                "description": "silently changed",
                "name": "A9 Version",
            }
        ),
        vendor_key,
        kid,
        jku,
    )
    return {
        "a1": card_to_dict(a1),
        "a1b": card_to_dict(a1b),
        "a2": a2,
        "a3": card_to_dict(a3),
        "a9-1.2.0": card_to_dict(v12),
        "a9-rollback": card_to_dict(v11),
        "a9-reuse": card_to_dict(reuse),
        "control": control_dict,
    }


def serve_card_attacks(settings: Settings) -> None:
    """Serve attack cards at /.well-known/agent-card.json selected by Host (ADR-0013 fetch path).

    Registry fetch_card only accepts that well-known path. Each attack is a DNS alias
    `<id>.vendor-x.local` on this container (see adversary_nodes).
    """
    from starlette.requests import Request

    cards = _attack_cards(settings)
    app = FastAPI(title="phase1-cards")

    def _card_for_host(host_header: str) -> dict[str, Any]:
        host = host_header.split(":")[0].lower()
        suffix = f".{VENDOR}"
        label = host[: -len(suffix)] if host.endswith(suffix) else "control"
        if label in ("card-attacks", ""):
            label = "control"
        return cards.get(label) or cards["control"]

    @app.get("/.well-known/agent-card.json")
    async def well_known(request: Request) -> JSONResponse:
        return JSONResponse(_card_for_host(request.headers.get("host") or ""))

    uvicorn.run(app, host=settings.bourse_bind_host, port=55520)
