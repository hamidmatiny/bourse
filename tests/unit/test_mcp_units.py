import pytest
from mcp import Client

from bourse.mcp_servers.units import build_server, convert_impl, parse_header_unit_impl
from tests.datasets import NAMES, csv_text, truth

TOL = 1e-9


@pytest.mark.parametrize("name", NAMES)
def test_header_units_match_truth(name: str) -> None:
    header = csv_text(name).splitlines()[0].split(",")
    got = [c.model_dump() for h in header if (c := parse_header_unit_impl(h).conversion)]
    want = truth(name)["units"]["conversions"]
    assert [g["column"] for g in got] == [w["column"] for w in want]
    for g, w in zip(got, want, strict=True):
        assert {k: g[k] for k in ("dimension", "from_unit", "to_unit")} == {
            k: w[k] for k in ("dimension", "from_unit", "to_unit")
        }
        assert g["factor"] == pytest.approx(w["factor"], abs=TOL)
        assert g["offset"] == pytest.approx(w["offset"], abs=TOL)


@pytest.mark.parametrize("name", ["measurements", "orders"])
def test_conversion_checks(name: str) -> None:
    for check in truth(name)["conversion_checks"]:
        out = convert_impl(check["value"], check["from_unit"], check["to_unit"])
        assert out.value == pytest.approx(check["expected"], abs=TOL)


@pytest.mark.parametrize(
    ("value", "src", "dst", "expected"),
    [
        (32.0, "F", "C", 0.0),
        (212.0, "degF", "degC", 100.0),
        (-40.0, "F", "C", -40.0),
        (0.0, "C", "K", 273.15),
        (100.0, "C", "F", 212.0),
        (1.0, "mi", "km", 1.609344),
        (1.0, "lb", "g", 453.59237),
        (90.0, "min", "h", 1.5),
    ],
)
def test_convert_affine_and_linear(value: float, src: str, dst: str, expected: float) -> None:
    assert convert_impl(value, src, dst).value == pytest.approx(expected, abs=TOL)


def test_convert_rejects_dimension_mismatch_and_unknown_units() -> None:
    with pytest.raises(ValueError, match="cannot convert"):
        convert_impl(1.0, "kg", "m")
    with pytest.raises(ValueError, match="unknown unit"):
        convert_impl(1.0, "furlong", "m")


@pytest.mark.parametrize("header", ["site", "sample_id", "weight", "kg", "temp_X"])
def test_headers_without_units(header: str) -> None:
    assert parse_header_unit_impl(header).conversion is None


async def test_mcp_round_trip_convert() -> None:
    async with Client(build_server()) as client:
        result = await client.call_tool(
            "convert", {"value": 212.0, "from_unit": "degF", "to_unit": "degC"}
        )
    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["value"] == pytest.approx(100.0, abs=TOL)
