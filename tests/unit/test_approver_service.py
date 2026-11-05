"""Approver entrypoint: shared asyncio loop, stub Socket Mode, no secrets in logs."""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from bourse.approval.redact import (
    classify_slack_connect_error,
    format_slack_env_presence,
    redact_secrets,
    slack_env_presence,
)
from bourse.approval.slack import FakeSlackClient, SlackApprover, StubSocketMode
from bourse.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_import_approval_modules_creates_no_client_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing approval modules must not construct aiohttp.ClientSession."""
    import aiohttp

    calls: list[str] = []

    class BoomSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append("ClientSession")
            raise AssertionError("aiohttp.ClientSession must not run at import time")

    monkeypatch.setattr(aiohttp, "ClientSession", BoomSession)
    for mod in (
        "bourse.approval.redact",
        "bourse.approval.slack",
        "bourse.approval.app",
        "bourse.approval.service",
    ):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)
    assert calls == []


def test_slack_approver_init_does_not_attach_socket() -> None:
    approver = SlackApprover(
        channel_id="C1",
        approver_ids=frozenset({"U1"}),
        client=FakeSlackClient(),
        bot_token="xoxb-SHOULD-NOT-ATTACH",
        app_token="xapp-SHOULD-NOT-ATTACH",
    )
    assert approver._socket is None
    assert approver.slack_status == "connecting"


@pytest.mark.asyncio
async def test_stub_socket_start_marks_connected() -> None:
    approver = SlackApprover(
        channel_id="C1",
        approver_ids=frozenset({"U1"}),
        client=FakeSlackClient(),
        socket_factory=StubSocketMode,
    )
    await approver.start()
    assert approver.slack_status == "connected"
    assert approver.is_socket_connected()
    await approver.stop()


def test_classify_slack_connect_error_classes() -> None:
    class ClientConnectorDNSError(OSError):
        pass

    assert (
        classify_slack_connect_error(
            ClientConnectorDNSError("Temporary failure in name resolution")
        )
        == "dns_error"
    )
    assert classify_slack_connect_error(RuntimeError("invalid_auth")) == "invalid_auth"
    assert classify_slack_connect_error(TimeoutError("timed out")) == "timeout"


def test_redact_secrets_strips_bot_and_app_tokens() -> None:
    raw = "fail xoxb-1234567890-ABCDEFG and xapp-1-A-B-C then done"
    out = redact_secrets(raw)
    assert "xoxb-" not in out
    assert "xapp-" not in out
    assert out.count("[REDACTED]") == 2
    assert "fail" in out and "then done" in out


def test_format_slack_env_presence_never_includes_values() -> None:
    settings = Settings(
        slack_bot_token=SecretStr("xoxb-SUPER-SECRET-TOKEN-VALUE"),
        slack_app_token=SecretStr("xapp-SUPER-SECRET-APP-TOKEN"),
        slack_channel_id="C0CF1LN2NCA",
        bourse_slack_approvers="U123",
    )
    presence = slack_env_presence(settings)
    assert presence == {
        "SLACK_BOT_TOKEN": "set",
        "SLACK_APP_TOKEN": "set",
        "SLACK_CHANNEL_ID": "set",
        "BOURSE_SLACK_APPROVERS": "set",
    }
    formatted = format_slack_env_presence(settings)
    assert "xoxb-" not in formatted
    assert "xapp-" not in formatted
    assert "SUPER-SECRET" not in formatted
    assert "SLACK_APP_TOKEN=set" in formatted
    empty = Settings(
        slack_bot_token=SecretStr(""),
        slack_app_token=SecretStr(""),
        slack_channel_id="",
        bourse_slack_approvers="",
    )
    assert "SLACK_APP_TOKEN=missing" in format_slack_env_presence(empty)


def test_approver_entrypoint_subprocess_reaches_listening_and_healthz() -> None:
    """Real CLI entrypoint + StubSocketMode; no Slack network."""
    port = _free_port()
    env = {
        **os.environ,
        "BOURSE_APPROVER_KEY": "test-key-not-a-slack-token",
        "BOURSE_APPROVER_STUB_SOCKET": "1",
        # Avoid pulling real Slack path if stub flag is missed.
        "SLACK_BOT_TOKEN": "",
        "SLACK_APP_TOKEN": "",
        "SLACK_CHANNEL_ID": "C_STUB",
        "BOURSE_SLACK_APPROVERS": "U_STUB",
    }
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "bourse",
            "approver",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--stub-socket",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 45.0
    buf = ""
    try:
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line:
                buf += line
                if "approver: listening" in buf:
                    break
            elif proc.poll() is not None:
                break
            else:
                time.sleep(0.05)
        assert "approver: listening" in buf, (
            f"never listening; exit={proc.poll()} out={buf[-2000:]}"
        )

        health: dict[str, object] | None = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
                if resp.status_code == 200:
                    health = resp.json()
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert health is not None, f"healthz never 200; out={buf[-2000:]}"
        assert health.get("slack") == "connected"
        assert "xox" not in buf.lower()
        assert "xapp-" not in buf.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
