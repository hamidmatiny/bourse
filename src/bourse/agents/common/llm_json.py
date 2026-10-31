"""Helpers for building LLM prompts from dataset content (never from Agent Card text)."""

import csv
import io
import json
from typing import Any

from bourse.llm.base import ChatMessage


def read_columns(csv_text: str) -> tuple[list[str], dict[str, list[str | None]]]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return [], {}
    header = rows[0]
    columns: dict[str, list[str | None]] = {h: [] for h in header}
    for row in rows[1:]:
        for i, h in enumerate(header):
            cell = row[i] if i < len(row) else ""
            columns[h].append(cell if cell != "" else None)
    return header, columns


def samples(columns: dict[str, list[str | None]], k: int = 5) -> dict[str, list[str]]:
    return {h: [v for v in vals if v is not None][:k] for h, vals in columns.items()}


def user_json(instruction: str, data: Any) -> ChatMessage:
    return ChatMessage(role="user", content=f"{instruction}\n\n{json.dumps(data, sort_keys=True)}")
