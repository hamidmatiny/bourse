"""Onboard 3 vendors (pinned JWKS from data/keys) and register the 5 agents by card_url."""

import json
from pathlib import Path
from typing import Any

import httpx

from bourse.agents.catalog import specs_from_settings
from bourse.agents.common.card import vendor_jwks_url
from bourse.config import Settings
from bourse.contracts.registry import RegistrationOutcome, VendorCreate, VendorKeys
from bourse.crypto.keys import DEFAULT_KEY_DIR
from bourse.stack.launcher import admin_token

VENDORS = {"vendor-a.local": "Vendor A", "vendor-b.local": "Vendor B", "vendor-c.local": "Vendor C"}


def vendor_body(domain: str, name: str, key_dir: Path = DEFAULT_KEY_DIR) -> dict[str, Any]:
    jwks = json.loads((key_dir / domain / "jwks.json").read_text())
    body = VendorCreate(
        domain=domain, name=name, keys=VendorKeys(jwks_url=vendor_jwks_url(domain), jwks=jwks)
    )
    return body.model_dump(mode="json")


def register_all(settings: Settings) -> tuple[list[list[str]], int, int]:
    """Return (table rows, accepted, total)."""
    base = settings.bourse_registry_url.rstrip("/")
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    rows = [["agent", "vendor", "version", "outcome", "state"]]
    accepted = 0
    with httpx.Client(timeout=30.0) as client:
        for domain, name in VENDORS.items():
            resp = client.post(f"{base}/vendors", json=vendor_body(domain, name), headers=headers)
            if resp.status_code not in (201, 409):
                raise RuntimeError(f"vendor {domain}: HTTP {resp.status_code} {resp.text[:200]}")
        specs = specs_from_settings(settings)
        for spec in specs.values():
            card_url = f"http://127.0.0.1:{spec.port}/.well-known/agent-card.json"
            resp = client.post(
                f"{base}/agents/register",
                json={"vendor_domain": spec.vendor_domain, "card_url": card_url},
            )
            outcome = RegistrationOutcome.model_validate(resp.json())
            ok = outcome.accepted
            accepted += ok
            label = "ACCEPTED" if ok else f"REJECTED:{outcome.reason}"
            if ok and outcome.unchanged:
                label = "ACCEPTED(unchanged)"
            rows.append(
                [
                    outcome.agent_id or spec.name,
                    spec.vendor_domain,
                    outcome.version or "-",
                    label,
                    outcome.state.value if outcome.state else "-",
                ]
            )
    return rows, accepted, len(specs)
