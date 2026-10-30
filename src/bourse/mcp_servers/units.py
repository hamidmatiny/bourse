"""Unit tools: parse_header_unit, convert (ADR-0015, ADR-0016). Own table, no pint.

Every unit maps to its dimension's canonical unit as canonical = value * factor + offset.
Headers carry the unit as the suffix after the last underscore (weight_lb, temp_F, dist_km);
suffix lookup is case-insensitive.
"""

from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from bourse.contracts.skills_io import CANONICAL_UNITS, Conversion, Dimension
from bourse.mcp_servers.common import check_size, run_limited

# symbol -> (dimension, factor, offset)
UNITS: dict[str, tuple[Dimension, float, float]] = {
    "mm": ("length", 0.001, 0.0),
    "cm": ("length", 0.01, 0.0),
    "m": ("length", 1.0, 0.0),
    "km": ("length", 1000.0, 0.0),
    "in": ("length", 0.0254, 0.0),
    "ft": ("length", 0.3048, 0.0),
    "yd": ("length", 0.9144, 0.0),
    "mi": ("length", 1609.344, 0.0),
    "mg": ("mass", 1e-6, 0.0),
    "g": ("mass", 0.001, 0.0),
    "kg": ("mass", 1.0, 0.0),
    "t": ("mass", 1000.0, 0.0),
    "oz": ("mass", 0.028349523125, 0.0),
    "lb": ("mass", 0.45359237, 0.0),
    "degC": ("temperature", 1.0, 0.0),
    "degF": ("temperature", 5.0 / 9.0, -160.0 / 9.0),
    "K": ("temperature", 1.0, -273.15),
    "ms": ("duration", 0.001, 0.0),
    "s": ("duration", 1.0, 0.0),
    "min": ("duration", 60.0, 0.0),
    "h": ("duration", 3600.0, 0.0),
    "d": ("duration", 86400.0, 0.0),
    "mL": ("volume", 0.001, 0.0),
    "L": ("volume", 1.0, 0.0),
    "m3": ("volume", 1000.0, 0.0),
    "floz": ("volume", 0.0295735295625, 0.0),
    "qt": ("volume", 0.946352946, 0.0),
    "gal": ("volume", 3.785411784, 0.0),
}

# lower-case alias -> symbol
ALIASES: dict[str, str] = {s.lower(): s for s in UNITS} | {
    "inch": "in",
    "feet": "ft",
    "mile": "mi",
    "tonne": "t",
    "lbs": "lb",
    "c": "degC",
    "celsius": "degC",
    "f": "degF",
    "fahrenheit": "degF",
    "kelvin": "K",
    "sec": "s",
    "secs": "s",
    "mins": "min",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "day": "d",
    "days": "d",
    "l": "L",
    "liter": "L",
    "litre": "L",
}


class HeaderUnitOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header: str
    conversion: Conversion | None


class ConvertOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    from_unit: str
    to_unit: str


def resolve_unit(name: str) -> str:
    symbol = ALIASES.get(name.strip().lower())
    if symbol is None:
        raise ValueError(f"unknown unit {name!r}")
    return symbol


def conversion_for(column: str, symbol: str) -> Conversion:
    dimension, factor, offset = UNITS[symbol]
    return Conversion(
        column=column,
        dimension=dimension,
        from_unit=symbol,
        to_unit=CANONICAL_UNITS[dimension],
        factor=factor,
        offset=offset,
    )


def parse_header_unit_impl(header: str) -> HeaderUnitOut:
    check_size(header)
    _, sep, suffix = header.rpartition("_")
    symbol = ALIASES.get(suffix.lower()) if sep else None
    return HeaderUnitOut(
        header=header, conversion=conversion_for(header, symbol) if symbol else None
    )


def convert_impl(value: float, from_unit: str, to_unit: str) -> ConvertOut:
    src, dst = resolve_unit(from_unit), resolve_unit(to_unit)
    src_dim, src_factor, src_offset = UNITS[src]
    dst_dim, dst_factor, dst_offset = UNITS[dst]
    if src_dim != dst_dim:
        raise ValueError(f"cannot convert {src_dim} ({src}) to {dst_dim} ({dst})")
    canonical = value * src_factor + src_offset
    return ConvertOut(value=(canonical - dst_offset) / dst_factor, from_unit=src, to_unit=dst)


def build_server() -> MCPServer[Any]:
    server = MCPServer("bourse-units", version="0.1.0")

    @server.tool()
    async def parse_header_unit(header: str) -> HeaderUnitOut:
        """Unit from a header suffix (e.g. weight_lb); conversion to the canonical unit or null."""
        return await run_limited(lambda: parse_header_unit_impl(header))

    @server.tool()
    async def convert(value: float, from_unit: str, to_unit: str) -> ConvertOut:
        """Convert a value between units of the same dimension (affine for temperature)."""
        return await run_limited(lambda: convert_impl(value, from_unit, to_unit))

    return server
