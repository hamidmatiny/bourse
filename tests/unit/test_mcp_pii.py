import pytest
from mcp import Client

from bourse.mcp_servers.pii import (
    build_server,
    classify,
    luhn_check_impl,
    luhn_valid,
    regex_scan_impl,
    sin_check_impl,
    sin_valid,
)
from tests.datasets import NAMES, csv_text, truth


def _columns(name: str) -> dict[str, list[str]]:
    import csv
    import io

    rows = list(csv.reader(io.StringIO(csv_text(name))))
    header, data = rows[0], rows[1:]
    return {h: [r[i] for r in data] for i, h in enumerate(header)}


@pytest.mark.parametrize("name", NAMES)
def test_regex_scan_matches_truth_for_every_column(name: str) -> None:
    expected = truth(name)["regex_candidates"]
    for column, values in _columns(name).items():
        got = regex_scan_impl(values).model_dump()["candidates"]
        assert got == expected[column], column


def test_luhn_and_sin_cases_from_fixtures() -> None:
    t = truth("people")
    cards = [c["value"] for c in t["luhn_cases"]]
    assert luhn_check_impl(cards).results == [c["valid"] for c in t["luhn_cases"]]
    sins = [c["value"] for c in t["sin_cases"]]
    assert sin_check_impl(sins).results == [c["valid"] for c in t["sin_cases"]]
    assert 0 < sum(c["valid"] for c in t["luhn_cases"]) < len(cards)
    assert 0 < sum(c["valid"] for c in t["sin_cases"]) < len(sins)


@pytest.mark.parametrize(
    ("number", "valid"),
    [
        ("4111 1111 1111 1111", True),
        ("4111-1111-1111-1111", True),
        ("4111111111111112", False),
        ("378282246310005", True),
        ("0", False),
        ("", False),
        ("00", True),
        ("4111 1111 1111 111a", False),
        ("79927398713", True),
    ],
)
def test_luhn_edges(number: str, valid: bool) -> None:
    assert luhn_valid(number) is valid


@pytest.mark.parametrize(
    ("number", "valid"),
    [
        ("046 454 286", False),  # Luhn-valid, but 0 is not an issued first digit
        ("846 454 280", False),  # first digit 8 is not used
        ("130 692 544", True),
        ("130 692 545", False),
        ("13069254", False),
        ("1306925440", False),
        ("130-692-544", True),
    ],
)
def test_sin_edges(number: str, valid: bool) -> None:
    assert sin_valid(number) is valid


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("a.b@example.com", "EMAIL"),
        ("(416) 555-0199", "PHONE"),
        ("+1 647 555 0142", "PHONE"),
        ("4111 1111 1111 1111", "CARD"),
        ("130 692 544", "SIN"),
        ("2024-01-05", None),
        ("call me at 416-555-0199", None),  # whole-value match only
        ("12345", None),
    ],
)
def test_classify(value: str, kind: str | None) -> None:
    assert classify(value) == kind


async def test_mcp_round_trip_luhn_check() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool(
            "luhn_check", {"numbers": ["4111 1111 1111 1111", "4111 1111 1111 1112"]}
        )
    assert not result.is_error
    assert result.structured_content == {"results": [True, False]}
