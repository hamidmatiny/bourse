"""Loaders for tests/fixtures/datasets."""

import json
from pathlib import Path
from typing import Any

DATASETS = Path(__file__).parent / "fixtures" / "datasets"
NAMES = ["people", "measurements", "orders"]


def csv_text(name: str) -> str:
    return (DATASETS / f"{name}.csv").read_text()


def truth(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATASETS / f"{name}.truth.json").read_text())
    return data
