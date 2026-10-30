"""Detached-payload JWS (RFC 7515) over the JCS card payload (A2A v1.0 §8.4; ADR-0012).

Verification policy:
- alg allowlist {ES256, EdDSA}; anything else (none, HS*, RS*, ...) -> UNSUPPORTED_ALG.
- Keys come only from the vendor's pinned JWKS. The card's `jku` is never fetched; if present it
  must equal the pinned jwks_url, else UNTRUSTED_JKU. Embedded keys (jwk, x5u, x5c) are
  rejected as UNTRUSTED_JKU.
- `crit`, `b64`, and key-selecting parameters in the unprotected header -> MALFORMED.
- A card is OK if at least one signature verifies.
"""

import base64
import binascii
import json
from typing import Any

from a2a.types import AgentCard, AgentCardSignature
from jwcrypto import jwk, jws
from jwcrypto.common import JWException

from bourse.contracts.registry import VendorKeys, VerifyResult
from bourse.crypto.jcs import CanonicalizationError, signing_payload
from bourse.crypto.keys import Alg, key_alg

ALLOWED_ALGS: frozenset[str] = frozenset({"ES256", "EdDSA"})
_EMBEDDED_KEY_PARAMS = ("jwk", "x5u", "x5c")
_UNPROTECTED_FORBIDDEN = ("alg", "kid", "jku", "jwk", "x5u", "x5c", "crit", "b64")
_KEY_TYPES: dict[str, tuple[str, str]] = {"ES256": ("EC", "P-256"), "EdDSA": ("OKP", "Ed25519")}

# When several signatures fail, report the most security-relevant failure.
_FAILURE_PRIORITY = (
    VerifyResult.UNTRUSTED_JKU,
    VerifyResult.UNSUPPORTED_ALG,
    VerifyResult.BAD_SIGNATURE,
    VerifyResult.UNKNOWN_KID,
    VerifyResult.MALFORMED,
)

__all__ = ["ALLOWED_ALGS", "VerifyResult", "sign_card", "verify_card"]


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not text or any(c in text for c in "=+/ \n"):
        raise ValueError("not unpadded base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_card(
    card: AgentCard, private_key: jwk.JWK, kid: str, jku: str | None, alg: Alg | None = None
) -> AgentCard:
    """Return a copy of `card` with one more detached-payload JWS in `signatures`."""
    alg = alg or key_alg(private_key)
    payload = signing_payload(card)
    header: dict[str, str] = {"alg": alg, "kid": kid, "typ": "JOSE"}
    if jku is not None:
        header["jku"] = jku
    token = jws.JWS(payload)
    token.allowed_algs = [alg]
    token.add_signature(private_key, alg=None, protected=json.dumps(header))
    protected, _, signature = str(token.serialize(compact=True)).split(".")
    signed = AgentCard()
    signed.CopyFrom(card)
    signed.signatures.append(AgentCardSignature(protected=protected, signature=signature))
    return signed


def _find_key(pinned: VendorKeys, kid: str) -> dict[str, Any] | None:
    for key in pinned.jwks["keys"]:
        if key.get("kid") == kid:
            found: dict[str, Any] = key
            return found
    return None


def _verify_one(sig: AgentCardSignature, payload: bytes, pinned: VendorKeys) -> VerifyResult:
    try:
        header = json.loads(b64url_decode(sig.protected))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return VerifyResult.MALFORMED
    if not isinstance(header, dict):
        return VerifyResult.MALFORMED
    if sig.HasField("header") and any(p in sig.header.fields for p in _UNPROTECTED_FORBIDDEN):
        return VerifyResult.MALFORMED
    alg = header.get("alg")
    if not isinstance(alg, str):
        return VerifyResult.MALFORMED
    if alg not in ALLOWED_ALGS:
        return VerifyResult.UNSUPPORTED_ALG
    if "crit" in header or "b64" in header:
        return VerifyResult.MALFORMED
    if any(p in header for p in _EMBEDDED_KEY_PARAMS):
        return VerifyResult.UNTRUSTED_JKU
    jku = header.get("jku")
    if jku is not None and jku != pinned.jwks_url:
        return VerifyResult.UNTRUSTED_JKU
    kid = header.get("kid")
    if not isinstance(kid, str):
        return VerifyResult.MALFORMED
    key_params = _find_key(pinned, kid)
    if key_params is None:
        return VerifyResult.UNKNOWN_KID
    kty, crv = _KEY_TYPES[alg]
    if key_params.get("kty") != kty or key_params.get("crv") != crv:
        return VerifyResult.BAD_SIGNATURE
    if key_params.get("alg") not in (None, alg):
        return VerifyResult.BAD_SIGNATURE
    try:
        b64url_decode(sig.signature)
    except (ValueError, binascii.Error):
        return VerifyResult.MALFORMED
    try:
        key = jwk.JWK(**key_params)
        token = jws.JWS()
        token.allowed_algs = [alg]
        token.deserialize(f"{sig.protected}.{b64url_encode(payload)}.{sig.signature}")
        token.verify(key)
    except jws.InvalidJWSSignature:
        return VerifyResult.BAD_SIGNATURE
    except (JWException, ValueError, TypeError):
        return VerifyResult.MALFORMED
    return VerifyResult.OK


def verify_card(card: AgentCard, pinned: VendorKeys) -> VerifyResult:
    if not card.signatures:
        return VerifyResult.NO_SIGNATURE
    try:
        payload = signing_payload(card)
    except CanonicalizationError:
        return VerifyResult.MALFORMED
    results = {_verify_one(sig, payload, pinned) for sig in card.signatures}
    if VerifyResult.OK in results:
        return VerifyResult.OK
    return next(r for r in _FAILURE_PRIORITY if r in results)
