"""Pinned shadow price table, config/prices.toml (ADR-0029). Shadow prices are not billed."""

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from bourse.contracts.metering import LlmUsageRecord


@dataclass(frozen=True)
class ModelPrice:
    in_per_1m: float
    out_per_1m: float


@dataclass(frozen=True)
class PriceTable:
    version: str
    tool_call_usd: float
    models: dict[str, ModelPrice] = field(default_factory=dict)

    def model_price(self, model: str) -> ModelPrice:
        """Unknown models are priced at the highest listed rates (conservative)."""
        if model in self.models:
            return self.models[model]
        return ModelPrice(
            max((p.in_per_1m for p in self.models.values()), default=0.0),
            max((p.out_per_1m for p in self.models.values()), default=0.0),
        )

    def usd(self, usage: Iterable[LlmUsageRecord], tool_calls: int) -> float:
        total = tool_calls * self.tool_call_usd
        for u in usage:
            price = self.model_price(u.model)
            total += u.tokens_in * price.in_per_1m / 1e6 + u.tokens_out * price.out_per_1m / 1e6
        return total


def load_prices(path: Path) -> PriceTable:
    raw = tomllib.loads(path.read_text())
    models = {
        name: ModelPrice(float(v["in_per_1m"]), float(v["out_per_1m"]))
        for name, v in raw.get("models", {}).items()
    }
    return PriceTable(str(raw["version"]), float(raw["tool_call_usd"]), models)
