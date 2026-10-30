"""Agent Card signing payload (ADR-0012).

Rule, identical to a2a-sdk 1.2.3 `a2a.utils.signing._canonicalize_agent_card`:
1. Serialize the AgentCard proto with `google.protobuf.json_format.MessageToDict` (JSON field
   names; proto3 explicit-presence semantics: unset implicit-presence scalars are omitted, set
   optional fields are kept even when false/0).
2. Remove the top-level `signatures` member.
3. Recursively drop empty strings, empty lists and empty objects (the SDK does the same).
4. Canonicalize with RFC 8785 (`rfc8785.dumps`) -> UTF-8 bytes.
"""

import hashlib
from typing import Any

import rfc8785
from a2a.types import AgentCard
from google.protobuf.json_format import MessageToDict, ParseDict

MAX_DEPTH = 128


class CanonicalizationError(ValueError):
    pass


def card_to_dict(card: AgentCard) -> dict[str, Any]:
    data: dict[str, Any] = MessageToDict(card)
    return data


def card_from_dict(data: dict[str, Any]) -> AgentCard:
    """Parse A2A v1.0 card JSON. Unknown fields are rejected (raises ValueError)."""
    card = AgentCard()
    try:
        ParseDict(data, card, ignore_unknown_fields=False)
    except Exception as exc:  # protobuf raises json_format.ParseError and TypeError variants
        raise ValueError(f"not a valid A2A v1.0 AgentCard: {exc}") from exc
    return card


def _clean_empty(value: Any, depth: int = 1) -> Any:
    if isinstance(value, dict | list) and depth > MAX_DEPTH:
        raise CanonicalizationError(f"nesting exceeds {MAX_DEPTH}")
    if isinstance(value, dict):
        cleaned = {k: c for k, v in value.items() if (c := _clean_empty(v, depth + 1)) is not None}
        return cleaned or None
    if isinstance(value, list):
        items = [c for v in value if (c := _clean_empty(v, depth + 1)) is not None]
        return items or None
    if isinstance(value, str) and not value:
        return None
    return value


def signing_payload(card: AgentCard) -> bytes:
    data = card_to_dict(card)
    data.pop("signatures", None)
    cleaned = _clean_empty(data) or {}
    try:
        return rfc8785.dumps(cleaned)
    except rfc8785.CanonicalizationError as exc:
        raise CanonicalizationError(str(exc)) from exc


def payload_digest(card: AgentCard) -> str:
    """SHA-256 of the signing payload: identifies card content independent of signatures."""
    return hashlib.sha256(signing_payload(card)).hexdigest()
