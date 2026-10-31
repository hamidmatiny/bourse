"""Agent Card built from the spec and signed with the vendor key (Phase 1 card_sign)."""

import json
from pathlib import Path

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from jwcrypto import jwk

from bourse.agents.common.spec import AgentSpec
from bourse.contracts.skills import SkillId
from bourse.crypto.card_sign import sign_card
from bourse.crypto.keys import DEFAULT_KEY_DIR, load_key, thumbprint_kid

SKILL_TEXT: dict[SkillId, tuple[str, list[str]]] = {
    SkillId.SCHEMA_PROFILE: ("Column dtypes, null and distinct counts.", ["schema", "profile"]),
    SkillId.STATS_COMPUTE: ("Mean, std, min, max, median of numeric columns.", ["stats"]),
    SkillId.PII_DETECT: ("Cell-level PII findings.", ["pii", "privacy"]),
    SkillId.UNITS_NORMALIZE: ("Unit conversions to canonical units.", ["units"]),
    SkillId.REPORT_COMPOSE: ("Short factual report with cited numbers.", ["report"]),
}


def vendor_jwks_url(vendor_domain: str) -> str:
    return f"https://{vendor_domain}/.well-known/jwks.json"


def build_card(spec: AgentSpec, base_url: str) -> AgentCard:
    return AgentCard(
        name=spec.display_name,
        description=spec.description,
        version=spec.version,
        supported_interfaces=[
            AgentInterface(
                url=base_url.rstrip("/") + "/", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        provider=AgentProvider(organization=spec.vendor_org, url=f"https://{spec.vendor_domain}"),
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=s.value, name=s.value, description=SKILL_TEXT[s][0], tags=SKILL_TEXT[s][1]
            )
            for s in spec.skills
        ],
    )


def latest_vendor_key(vendor_domain: str, key_dir: Path = DEFAULT_KEY_DIR) -> jwk.JWK:
    """Newest private key written by `bourse keys gen --vendor <domain>`."""
    keys = sorted(
        (key_dir / vendor_domain).glob("*.private.jwk.json"), key=lambda p: p.stat().st_mtime
    )
    if not keys:
        raise FileNotFoundError(
            f"no private key for {vendor_domain}; run: bourse keys gen --vendor {vendor_domain}"
        )
    return load_key(json.loads(keys[-1].read_text()))


def signed_card(spec: AgentSpec, base_url: str, key: jwk.JWK) -> AgentCard:
    return sign_card(
        build_card(spec, base_url), key, thumbprint_kid(key), vendor_jwks_url(spec.vendor_domain)
    )
