import hashlib
import hmac
import json
from typing import Any

import jwt
import pytest
from a2a.types import AgentCard, AgentCardSignature
from a2a.utils.signing import (
    InvalidSignaturesError,
    ProtectedHeader,
    create_agent_card_signer,
    create_signature_verifier,
)
from pydantic import ValidationError

from bourse.contracts.registry import VendorKeys, VerifyResult
from bourse.crypto.card_sign import b64url_decode, b64url_encode, sign_card, verify_card
from bourse.crypto.jcs import card_from_dict, signing_payload
from bourse.crypto.keys import private_jwk, public_jwk
from tests.registry_helpers import Party, load_card, make_party

VENDOR = make_party("vendor-a.local")
OTHER = make_party("attacker.local")


def _card() -> AgentCard:
    return card_from_dict(load_card("honest-a-profiler.json"))


def _with_signature(card: AgentCard, header: dict[str, Any], signature: str = "AAAA") -> AgentCard:
    out = AgentCard()
    out.CopyFrom(card)
    protected = b64url_encode(json.dumps(header).encode())
    out.signatures.append(AgentCardSignature(protected=protected, signature=signature))
    return out


def _header(card: AgentCard, index: int = 0) -> dict[str, Any]:
    header: dict[str, Any] = json.loads(b64url_decode(card.signatures[index].protected))
    return header


def test_es256_round_trip() -> None:
    signed = sign_card(_card(), VENDOR.key, VENDOR.kid, VENDOR.jwks_url)
    assert verify_card(signed, VENDOR.keys) is VerifyResult.OK
    assert _header(signed) == {
        "alg": "ES256",
        "kid": VENDOR.kid,
        "typ": "JOSE",
        "jku": VENDOR.jwks_url,
    }


def test_eddsa_round_trip() -> None:
    party = make_party("vendor-e.local", "EdDSA")
    signed = sign_card(_card(), party.key, party.kid, None)
    assert _header(signed)["alg"] == "EdDSA"
    assert verify_card(signed, party.keys) is VerifyResult.OK


def test_sign_does_not_mutate_input() -> None:
    card = _card()
    sign_card(card, VENDOR.key, VENDOR.kid, None)
    assert len(card.signatures) == 0


def test_no_signature() -> None:
    assert verify_card(_card(), VENDOR.keys) is VerifyResult.NO_SIGNATURE


def test_tampered_payload_is_bad_signature() -> None:
    signed = sign_card(_card(), VENDOR.key, VENDOR.kid, None)
    signed.description = "edited after signing"
    assert verify_card(signed, VENDOR.keys) is VerifyResult.BAD_SIGNATURE


def test_attacker_key_is_unknown_kid() -> None:
    signed = sign_card(_card(), OTHER.key, OTHER.kid, None)
    assert verify_card(signed, VENDOR.keys) is VerifyResult.UNKNOWN_KID


def test_attacker_key_with_spoofed_kid_is_bad_signature() -> None:
    signed = sign_card(_card(), OTHER.key, VENDOR.kid, None)
    assert verify_card(signed, VENDOR.keys) is VerifyResult.BAD_SIGNATURE


def test_jku_must_equal_pinned_url_and_is_never_fetched() -> None:
    swapped = sign_card(_card(), OTHER.key, VENDOR.kid, "https://attacker.local/jwks.json")
    assert verify_card(swapped, VENDOR.keys) is VerifyResult.UNTRUSTED_JKU
    # Even a correct signature with a foreign jku is rejected.
    honest_key_foreign_jku = sign_card(_card(), VENDOR.key, VENDOR.kid, "https://x.test/j")
    assert verify_card(honest_key_foreign_jku, VENDOR.keys) is VerifyResult.UNTRUSTED_JKU


@pytest.mark.parametrize("param", ["jwk", "x5u", "x5c"])
def test_embedded_key_params_rejected(param: str) -> None:
    header = {"alg": "ES256", "kid": VENDOR.kid, param: "x"}
    assert verify_card(_with_signature(_card(), header), VENDOR.keys) is VerifyResult.UNTRUSTED_JKU


@pytest.mark.parametrize("alg", ["none", "HS256", "RS256", "PS256", "ES384", "ES256K"])
def test_disallowed_algs(alg: str) -> None:
    header = {"alg": alg, "kid": VENDOR.kid}
    card = _with_signature(_card(), header, signature="AAAA" if alg != "none" else "")
    assert verify_card(card, VENDOR.keys) is VerifyResult.UNSUPPORTED_ALG


def test_hs256_with_public_key_as_secret_rejected() -> None:
    # Classic alg confusion: HMAC keyed with the vendor's public JWK JSON.
    secret = json.dumps(public_jwk(VENDOR.key)).encode()
    payload = signing_payload(_card())
    # PyJWT 2.15 refuses JWK-shaped HMAC secrets, so the attacker token is built by hand.
    protected = b64url_encode(json.dumps({"alg": "HS256", "kid": VENDOR.kid}).encode())
    signing_input = f"{protected}.{b64url_encode(payload)}".encode()
    signature = b64url_encode(hmac.new(secret, signing_input, hashlib.sha256).digest())
    card = _card()
    card.signatures.append(AgentCardSignature(protected=protected, signature=signature))
    assert verify_card(card, VENDOR.keys) is VerifyResult.UNSUPPORTED_ALG


@pytest.mark.parametrize(
    "protected",
    [
        "not base64!",
        b64url_encode(b"not json"),
        b64url_encode(b"[1, 2]"),
        b64url_encode(b'{"kid": "k"}'),
        b64url_encode(b'{"alg": 5, "kid": "k"}'),
        "",
    ],
    ids=["not-b64", "not-json", "json-array", "no-alg", "alg-not-string", "empty"],
)
def test_malformed_protected_header(protected: str) -> None:
    card = _card()
    card.signatures.append(AgentCardSignature(protected=protected, signature="AAAA"))
    assert verify_card(card, VENDOR.keys) is VerifyResult.MALFORMED


def test_missing_kid_and_crit_are_malformed() -> None:
    assert (
        verify_card(_with_signature(_card(), {"alg": "ES256"}), VENDOR.keys)
        is VerifyResult.MALFORMED
    )
    crit = {"alg": "ES256", "kid": VENDOR.kid, "crit": ["exp"], "exp": 1}
    assert verify_card(_with_signature(_card(), crit), VENDOR.keys) is VerifyResult.MALFORMED


def test_unprotected_header_cannot_select_key() -> None:
    signed = sign_card(_card(), VENDOR.key, VENDOR.kid, None)
    signed.signatures[0].header.update({"kid": OTHER.kid})
    assert verify_card(signed, VENDOR.keys) is VerifyResult.MALFORMED


def test_alg_key_type_mismatch_is_bad_signature() -> None:
    signed = sign_card(_card(), VENDOR.key, VENDOR.kid, None)
    header = _header(signed) | {"alg": "EdDSA"}
    forged = _with_signature(_card(), header, signed.signatures[0].signature)
    assert verify_card(forged, VENDOR.keys) is VerifyResult.BAD_SIGNATURE


def test_one_valid_signature_is_enough() -> None:
    first = sign_card(_card(), OTHER.key, OTHER.kid, None)
    both = sign_card(first, VENDOR.key, VENDOR.kid, None)
    assert len(both.signatures) == 2
    assert verify_card(both, VENDOR.keys) is VerifyResult.OK


def test_key_rotation_second_pinned_key() -> None:
    new_key = make_party("vendor-a.local")
    pinned = VendorKeys(
        jwks_url=VENDOR.jwks_url,
        jwks={"keys": [public_jwk(VENDOR.key), public_jwk(new_key.key)]},
    )
    signed = sign_card(_card(), new_key.key, new_key.kid, None)
    assert verify_card(signed, pinned) is VerifyResult.OK


def test_pinned_jwks_rejects_private_members() -> None:
    with pytest.raises(ValidationError, match="private"):
        VendorKeys(jwks_url=VENDOR.jwks_url, jwks={"keys": [private_jwk(VENDOR.key)]})


# --- Independent verifier: PyJWT ---------------------------------------------------------


def test_our_signature_verifies_with_pyjwt() -> None:
    card = sign_card(_card(), VENDOR.key, VENDOR.kid, VENDOR.jwks_url)
    sig = card.signatures[0]
    payload = signing_payload(card)
    token = f"{sig.protected}.{b64url_encode(payload)}.{sig.signature}"
    key = jwt.PyJWK(public_jwk(VENDOR.key))
    decoded = jwt.PyJWS().decode_complete(token, key=key.key, algorithms=["ES256"])
    assert decoded["payload"] == payload
    assert decoded["header"]["kid"] == VENDOR.kid


def test_pyjwt_signature_verifies_with_ours() -> None:
    card = _card()
    key = jwt.PyJWK(private_jwk(VENDOR.key))
    token = jwt.PyJWS().encode(
        signing_payload(card),
        key.key,
        algorithm="ES256",
        headers={"kid": VENDOR.kid, "typ": "JOSE", "jku": VENDOR.jwks_url},
    )
    protected, _, signature = token.split(".")
    card.signatures.append(AgentCardSignature(protected=protected, signature=signature))
    assert verify_card(card, VENDOR.keys) is VerifyResult.OK


# --- a2a-sdk 1.2.3 interop (a2a.utils.signing) ---------------------------------------------


def _sdk_verifier(party: Party) -> Any:
    public = jwt.PyJWK(public_jwk(party.key))

    def key_provider(kid: str | None, jku: str | None) -> jwt.PyJWK:
        assert kid == party.kid
        return public

    return create_signature_verifier(key_provider, ["ES256"])


def test_sdk_signature_verifies_with_ours() -> None:
    header: ProtectedHeader = {
        "alg": "ES256",
        "kid": VENDOR.kid,
        "jku": VENDOR.jwks_url,
        "typ": "JOSE",
    }
    signer = create_agent_card_signer(jwt.PyJWK(private_jwk(VENDOR.key)), header)
    card = signer(_card())
    assert verify_card(card, VENDOR.keys) is VerifyResult.OK


def test_our_signature_verifies_with_sdk() -> None:
    card = sign_card(_card(), VENDOR.key, VENDOR.kid, VENDOR.jwks_url)
    _sdk_verifier(VENDOR)(card)  # raises on failure


def test_sdk_rejects_our_tampered_card() -> None:
    card = sign_card(_card(), VENDOR.key, VENDOR.kid, None)
    card.description = "edited"
    with pytest.raises(InvalidSignaturesError):
        _sdk_verifier(VENDOR)(card)


def test_sdk_round_trip_non_ascii_card() -> None:
    card = card_from_dict(load_card("honest-b-pii.json"))
    party = make_party("vendor-b.local")
    ours = sign_card(card, party.key, party.kid, None)
    _sdk_verifier(party)(ours)
    header: ProtectedHeader = {"alg": "ES256", "kid": party.kid, "jku": None, "typ": "JOSE"}
    theirs = create_agent_card_signer(jwt.PyJWK(private_jwk(party.key)), header)(
        card_from_dict(load_card("honest-b-pii.json"))
    )
    assert verify_card(theirs, party.keys) is VerifyResult.OK
