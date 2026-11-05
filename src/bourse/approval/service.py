"""Approver process entrypoint: one asyncio loop for HTTP + Slack Socket Mode (ADR-0048).

Importing this module must not construct aiohttp / Socket Mode clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import uvicorn

from bourse.approval.app import create_app
from bourse.approval.redact import format_slack_env_presence, redact_secrets
from bourse.approval.select import missing_slack_env, select_approver
from bourse.approval.slack import SlackApprover, StubSocketMode
from bourse.config import Settings, get_settings

log = logging.getLogger(__name__)

# Set by tests / `bourse approver serve --stub-socket` — never talks to Slack.
STUB_ENV = "BOURSE_APPROVER_STUB_SOCKET"


def _listening_marker() -> None:
    """Printed once HTTP is accepting; subprocess tests wait for this line."""
    print("approver: listening", flush=True)


async def run_approver(
    *,
    host: str,
    port: int,
    settings: Settings | None = None,
    stub_socket: bool | None = None,
) -> None:
    """Build Slack handlers inside this coroutine, then serve HTTP on the same loop."""
    settings = settings or get_settings()
    stub = (
        bool(stub_socket)
        if stub_socket is not None
        else os.environ.get(STUB_ENV, "").strip() in {"1", "true", "yes"}
    )
    log.info("slack env: %s", format_slack_env_presence(settings))

    if stub:
        from bourse.approval.slack import FakeSlackClient

        approver: Any = SlackApprover(
            channel_id=settings.slack_channel_id.strip() or "C_STUB",
            approver_ids=settings.slack_approver_ids or frozenset({"U_STUB"}),
            client=FakeSlackClient(),
            bot_token="stub",
            app_token="stub",
            socket_factory=StubSocketMode,
        )
    else:
        selected = select_approver(settings, tty=False, start_socket=False, prefer_http=False)
        if not isinstance(selected, SlackApprover):
            missing = missing_slack_env(settings)
            log.error(
                "approver requires SlackApprover; got %s; missing %s",
                type(selected).__name__,
                ", ".join(missing) or "(none)",
            )
            # Still serve /healthz so compose can report the reason.
            status = {
                "ok": False,
                "slack": "error",
                "detail": f"not_slack_approver missing={','.join(missing)}",
                "approver": type(selected).__name__,
            }

            def unconfigured_status() -> dict[str, Any]:
                return dict(status)

            app = create_app(
                approver=selected,
                service_key=settings.bourse_approver_key.get_secret_value(),
                status_fn=unconfigured_status,
            )
            config = uvicorn.Config(app, host=host, port=port, log_level="info")
            server = uvicorn.Server(config)
            _listening_marker()
            await server.serve()
            return
        approver = selected

    def status_fn() -> dict[str, Any]:
        return {
            "ok": approver.slack_status == "connected",
            "slack": approver.slack_status,
            "detail": approver.slack_detail,
            "approver": type(approver).__name__,
            "socket_connected": approver.is_socket_connected(),
        }

    app = create_app(
        approver=approver,
        service_key=settings.bourse_approver_key.get_secret_value(),
        status_fn=status_fn,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)

    async def connect() -> None:
        try:
            await approver.start()
            log.info("slack socket status=%s", approver.slack_status)
        except Exception as exc:  # keep HTTP up so /healthz shows error
            log.error("slack connect failed: %s", redact_secrets(str(exc)))

    connect_task = asyncio.create_task(connect())
    _listening_marker()
    try:
        await server.serve()
    finally:
        connect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connect_task
        await approver.stop()


def serve(
    *,
    host: str = "0.0.0.0",
    port: int = 55310,
    stub_socket: bool = False,
) -> None:
    """CLI / compose entry: `asyncio.run` owns the single shared loop."""
    asyncio.run(
        run_approver(
            host=host,
            port=port,
            stub_socket=True if stub_socket else None,
        )
    )
