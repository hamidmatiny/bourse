import json
from pathlib import Path
from typing import Any

import httpx
import respx

from bourse.llm.probe import RESIDENT_LIMIT_BYTES, run_probe
from tests.unit.fakes import OLLAMA, make_client, ollama_submit, request_json


def _mock_ollama(ps_sizes: list[int]) -> tuple[respx.Router, list[dict[str, Any]]]:
    mock = respx.Router(assert_all_called=False)
    chat_bodies: list[dict[str, Any]] = []

    def chat(request: httpx.Request) -> httpx.Response:
        body = request_json(request)
        chat_bodies.append(body)
        if "format" in body:
            return httpx.Response(501, text="not implemented")
        return ollama_submit({"answer": 5})

    mock.get(f"{OLLAMA}/api/version").mock(
        return_value=httpx.Response(200, json={"version": "0.40.6"})
    )
    mock.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": "qwen3.5:27b-q4_K_M"}, {"name": "gemma4:12b"}]}
        )
    )
    mock.post(f"{OLLAMA}/api/chat").mock(side_effect=chat)
    mock.get(f"{OLLAMA}/api/ps").mock(
        return_value=httpx.Response(
            200, json={"models": [{"name": f"m{i}", "size": s} for i, s in enumerate(ps_sizes)]}
        )
    )
    return mock, chat_bodies


async def test_probe_records_501_and_resident_over_limit(tmp_path: Path) -> None:
    mock, chat_bodies = _mock_ollama([17_000_000_000, 9_000_000_000])
    report, written = await run_probe(make_client(mock), OLLAMA, "gemma4:12b", tmp_path)
    assert report.ollama_version == "0.40.6"
    assert report.models == ("qwen3.5:27b-q4_K_M", "gemma4:12b")
    assert report.format_json_status == 501
    assert report.tool_call_ok is True
    assert report.think_false_ok is True
    assert report.resident_bytes == 26_000_000_000
    assert report.resident_bytes >= RESIDENT_LIMIT_BYTES
    assert report.resident_ok is False
    assert all(b["think"] is False for b in chat_bodies)
    assert written is not None
    assert written.parent == tmp_path
    assert written.name.startswith("probe-")
    saved = json.loads(written.read_text())
    assert saved["format_json_status"] == 501
    assert saved["model"] == "gemma4:12b"


async def test_probe_resident_ok_under_limit() -> None:
    mock, _ = _mock_ollama([14_000_000_000, 8_000_000_000])
    report, written = await run_probe(make_client(mock), OLLAMA, "gemma4:12b", None)
    assert report.resident_ok is True
    assert written is None


async def test_probe_resident_fails_with_three_models() -> None:
    mock, _ = _mock_ollama([1_000_000_000, 1_000_000_000, 1_000_000_000])
    report, _ = await run_probe(make_client(mock), OLLAMA, "gemma4:12b", None)
    assert report.resident_ok is False
