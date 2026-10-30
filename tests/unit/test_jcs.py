import hashlib
import json
from typing import Any

import pytest
import rfc8785
from a2a.utils.signing import _canonicalize_agent_card

from bourse.crypto.jcs import (
    CanonicalizationError,
    card_from_dict,
    payload_digest,
    signing_payload,
)
from tests.registry_helpers import FIXTURE_NAMES, load_card, make_party


def test_rfc8785_key_order_is_utf16_code_units() -> None:
    # RFC 8785 section 3.2.3 example. Code point order would put U+FB33 before U+1F600.
    # Keys are built with chr() so no editor or formatter can normalize them.
    euro, cr, dalet, one, emoji, ctrl, o_uml = (
        chr(c) for c in (0x20AC, 0x0D, 0xFB33, 0x31, 0x1F600, 0x80, 0xF6)
    )
    data = {euro: "e", cr: "c", dalet: "d", one: "1", emoji: "g", ctrl: "x", o_uml: "o"}
    keys = list(json.loads(rfc8785.dumps(data)).keys())
    assert keys == [cr, one, ctrl, o_uml, euro, emoji, dalet]


def test_rfc8785_section_3_2_2_example() -> None:
    data: dict[str, Any] = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        # RFC source: "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/" (built with chr()
        # so formatters cannot rewrite the escapes).
        "string": "".join(
            chr(c)
            for c in (0x20AC, 0x24, 0x0F, 0x0A, 0x41, 0x27, 0x42, 0x22, 0x5C, 0x5C, 0x22, 0x2F)
        ),
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert rfc8785.dumps(data) == expected.encode("utf-8")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (9007199254740992.0, "9007199254740992"),
        (295147905179352830000.0, "295147905179352830000"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (-1.5, "-1.5"),
    ],
)
def test_rfc8785_number_formatting(value: float, expected: str) -> None:
    assert rfc8785.dumps(value) == expected.encode()


def test_rfc8785_string_escaping() -> None:
    assert rfc8785.dumps("\u0001\t\u001f\u007fé/") == '"\\u0001\\t\\u001f\u007fé/"'.encode()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_payload_matches_a2a_sdk(name: str) -> None:
    card = card_from_dict(load_card(name))
    assert signing_payload(card) == _canonicalize_agent_card(card).encode("utf-8")


def test_payload_excludes_signatures_and_matches_sdk_when_signed() -> None:
    party = make_party("vendor-a.local")
    unsigned = card_from_dict(load_card("honest-a-profiler.json"))
    signed = card_from_dict(party.sign(load_card("honest-a-profiler.json")))
    assert len(signed.signatures) == 1
    assert signing_payload(signed) == signing_payload(unsigned)
    assert signing_payload(signed) == _canonicalize_agent_card(signed).encode("utf-8")
    assert b"signatures" not in signing_payload(signed)


def test_empty_values_dropped_like_sdk() -> None:
    data: dict[str, Any] = load_card("honest-a-stats.json")
    data["documentationUrl"] = ""
    data["skills"][0]["examples"] = []
    data["capabilities"]["extensions"] = [{"uri": "urn:x", "params": {"a": {}, "n": 1}}]
    card = card_from_dict(data)
    payload = json.loads(signing_payload(card))
    assert "documentationUrl" not in payload
    assert "examples" not in payload["skills"][0]
    assert payload["capabilities"]["extensions"] == [{"params": {"n": 1}, "uri": "urn:x"}]
    assert signing_payload(card) == _canonicalize_agent_card(card).encode("utf-8")


def test_digest_is_sha256_of_payload() -> None:
    card = card_from_dict(load_card("honest-b-pii.json"))
    assert payload_digest(card) == hashlib.sha256(signing_payload(card)).hexdigest()


def test_deep_nesting_rejected() -> None:
    nested: dict[str, Any] = {"v": 1}
    for _ in range(200):
        nested = {"n": nested}
    data = load_card("honest-a-stats.json")
    data["capabilities"]["extensions"] = [{"uri": "urn:x", "params": nested}]
    with pytest.raises((CanonicalizationError, ValueError)):
        signing_payload(card_from_dict(data))


def test_unknown_card_field_rejected() -> None:
    data = load_card("honest-a-stats.json")
    data["notAField"] = 1
    with pytest.raises(ValueError, match="AgentCard"):
        card_from_dict(data)
