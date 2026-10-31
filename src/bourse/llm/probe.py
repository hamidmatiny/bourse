"""Startup capability probe for the Ollama server (ADR-0006).

Records: server version, installed models, HTTP status of a `format: "json"` request
(0.40.6 returns 501), whether a `submit` tool call works with think=false, and resident
model size from /api/ps (must be < 26 GB with at most 2 models loaded).
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx

from bourse.contracts.probe import ProbeReport
from bourse.runtime.deadline import DeadlineExceeded, deadline

RESIDENT_LIMIT_BYTES = 26 * 10**9
MAX_RESIDENT_MODELS = 2

_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": "Submit the final answer.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
        },
    },
}


async def run_probe(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    out_dir: Path | None = None,
    deadline_s: float = 120.0,
    num_ctx: int = 8192,
) -> tuple[ProbeReport, Path | None]:
    base = base_url.rstrip("/")

    async with deadline(15.0):
        version_resp = await client.get(f"{base}/api/version")
    version_resp.raise_for_status()
    version = str(version_resp.json().get("version") or "") or None

    async with deadline(15.0):
        tags_resp = await client.get(f"{base}/api/tags")
    tags_resp.raise_for_status()
    models = tuple(str(m.get("name", "")) for m in tags_resp.json().get("models") or [])

    format_status: int | None
    try:
        async with deadline(deadline_s):
            fmt_resp = await client.post(
                f"{base}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": 'Reply with {"ok": true}.'}],
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "options": {"num_predict": 16, "num_ctx": num_ctx},
                },
            )
        format_status = fmt_resp.status_code
    except (httpx.TransportError, DeadlineExceeded):
        format_status = None

    tool_call_ok = False
    think_false_ok = False
    latency_ms: float | None = None
    try:
        started = time.monotonic()
        async with deadline(deadline_s):
            tool_resp = await client.post(
                f"{base}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Answer only by calling the submit tool. No text reply.",
                        },
                        {
                            "role": "user",
                            "content": "What is 2 + 3? Call the submit tool with the answer.",
                        },
                    ],
                    "tools": [_PROBE_TOOL],
                    "stream": False,
                    "think": False,
                    "options": {
                        "num_predict": 128,
                        "temperature": 0,
                        "seed": 0,
                        "num_ctx": num_ctx,
                    },
                },
            )
        latency_ms = (time.monotonic() - started) * 1000
        if tool_resp.status_code == 200:
            message: dict[str, Any] = tool_resp.json().get("message") or {}
            calls = message.get("tool_calls") or []
            tool_call_ok = any(c.get("function", {}).get("name") == "submit" for c in calls)
            think_false_ok = not message.get("thinking")
    except (httpx.TransportError, DeadlineExceeded):
        pass

    async with deadline(15.0):
        ps_resp = await client.get(f"{base}/api/ps")
    ps_resp.raise_for_status()
    loaded = ps_resp.json().get("models") or []
    resident_bytes = sum(int(m.get("size") or 0) for m in loaded)
    resident_ok = resident_bytes < RESIDENT_LIMIT_BYTES and len(loaded) <= MAX_RESIDENT_MODELS

    report = ProbeReport(
        ollama_version=version,
        models=models,
        format_json_status=format_status,
        tool_call_ok=tool_call_ok,
        think_false_ok=think_false_ok,
        resident_bytes=resident_bytes,
        resident_ok=resident_ok,
        latency_ms=latency_ms,
    )
    written: Path | None = None
    if out_dir is not None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        written = out_dir / f"probe-{stamp}.json"
        await anyio.Path(out_dir).mkdir(parents=True, exist_ok=True)
        payload = {"model": model, "base_url": base, **report.model_dump(mode="json")}
        await anyio.Path(written).write_text(json.dumps(payload, indent=2) + "\n")
    return report, written
