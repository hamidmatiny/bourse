"""Per-agent proxy keys from BOURSE_METER_KEYS ("agent_id:key,agent_id:key"); never committed."""

import hmac
from collections.abc import Mapping

MIN_KEY_LENGTH = 16


def parse_keys(raw: str) -> dict[str, str]:
    """Return key -> agent_id. Raises ValueError on malformed entries or duplicate keys."""
    keys: dict[str, str] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        agent_id, sep, key = entry.partition(":")
        if not sep or not agent_id or len(key) < MIN_KEY_LENGTH:
            raise ValueError(
                "BOURSE_METER_KEYS entries must be agent_id:key "
                f"with keys of at least {MIN_KEY_LENGTH} characters"
            )
        if key in keys:
            raise ValueError("duplicate key in BOURSE_METER_KEYS")
        keys[key] = agent_id
    return keys


def agent_for(authorization: str | None, keys: Mapping[str, str]) -> str | None:
    """Constant-time lookup of the agent behind a `Bearer <key>` header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    presented = authorization.removeprefix("Bearer ").encode()
    found: str | None = None
    for key, agent_id in keys.items():
        if hmac.compare_digest(presented, key.encode()):
            found = agent_id
    return found
