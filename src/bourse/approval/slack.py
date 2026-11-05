"""SlackApprover via slack-bolt Socket Mode (ADR-0048)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from bourse.approval.facts import NOTIFY_TRIGGERS, format_summary
from bourse.approval.message import (
    APPROVE_ACTION,
    DENY_ACTION,
    build_blocks,
    build_fallback_text,
    wants_buttons,
)
from bourse.contracts.approval import ApprovalDecision, ApprovalRequest

log = logging.getLogger(__name__)


class SlackTransportError(RuntimeError):
    """Slack API unreachable or returned an error."""


class SlackClient(Protocol):
    async def chat_post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]: ...

    async def chat_update(
        self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]: ...

    async def chat_post_ephemeral(
        self, *, channel: str, user: str, text: str
    ) -> Mapping[str, Any]: ...


@dataclass
class FakeSlackClient:
    """In-memory Slack client for unit tests (no network)."""

    fail: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    ephemerals: list[dict[str, Any]] = field(default_factory=list)
    _n: int = 0

    async def chat_post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]:
        if self.fail:
            raise SlackTransportError("fake slack down")
        self._n += 1
        ts = f"1000.{self._n:06d}"
        payload = {"channel": channel, "text": text, "blocks": blocks, "ts": ts}
        self.messages.append(payload)
        return {"ok": True, "ts": ts, "channel": channel}

    async def chat_update(
        self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]:
        if self.fail:
            raise SlackTransportError("fake slack down")
        payload = {"channel": channel, "ts": ts, "text": text, "blocks": blocks}
        self.updates.append(payload)
        return {"ok": True, "ts": ts, "channel": channel}

    async def chat_post_ephemeral(self, *, channel: str, user: str, text: str) -> Mapping[str, Any]:
        if self.fail:
            raise SlackTransportError("fake slack down")
        payload = {"channel": channel, "user": user, "text": text}
        self.ephemerals.append(payload)
        return {"ok": True}


class BoltSlackClient:
    """Thin async wrapper over slack_sdk AsyncWebClient."""

    def __init__(self, bot_token: str) -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        self._client = AsyncWebClient(token=bot_token)

    async def chat_post_message(
        self, *, channel: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]:
        try:
            resp = await self._client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        except Exception as exc:
            from bourse.approval.redact import redact_secrets

            raise SlackTransportError(redact_secrets(str(exc))) from exc
        if not resp.get("ok"):
            raise SlackTransportError(str(resp.get("error", "chat_postMessage failed")))
        return {"ok": True, "ts": resp["ts"], "channel": resp.get("channel", channel)}

    async def chat_update(
        self, *, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> Mapping[str, Any]:
        try:
            resp = await self._client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        except Exception as exc:
            from bourse.approval.redact import redact_secrets

            raise SlackTransportError(redact_secrets(str(exc))) from exc
        if not resp.get("ok"):
            raise SlackTransportError(str(resp.get("error", "chat_update failed")))
        return {"ok": True, "ts": resp.get("ts", ts), "channel": channel}

    async def chat_post_ephemeral(self, *, channel: str, user: str, text: str) -> Mapping[str, Any]:
        try:
            resp = await self._client.chat_postEphemeral(channel=channel, user=user, text=text)
        except Exception as exc:
            from bourse.approval.redact import redact_secrets

            raise SlackTransportError(redact_secrets(str(exc))) from exc
        if not resp.get("ok"):
            raise SlackTransportError(str(resp.get("error", "chat_postEphemeral failed")))
        return {"ok": True}


@dataclass
class _Pending:
    req: ApprovalRequest
    event: asyncio.Event
    decision: ApprovalDecision | None = None
    channel: str = ""
    ts: str = ""


class StubSocketMode:
    """In-process Socket Mode stand-in for unit tests (no network, no aiohttp)."""

    def __init__(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect_async(self) -> None:
        self._connected = True

    async def close_async(self) -> None:
        self._connected = False


class SlackApprover:
    """Socket Mode Slack approver. Injectable client for unit tests.

    AsyncApp / AsyncSocketModeHandler are built only inside `start()` while an event
    loop is running — never at import or __init__ time (ADR-0048).
    """

    def __init__(
        self,
        *,
        channel_id: str,
        approver_ids: frozenset[str],
        client: SlackClient,
        bot_token: str = "",
        app_token: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        start_socket: bool = False,  # ignored; kept for call-site back-compat
        socket_factory: Callable[[], Any] | None = None,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        del start_socket  # construction must never bind aiohttp to a loop
        self.channel_id = channel_id
        self.approver_ids = approver_ids
        self.client = client
        self.bot_token = bot_token
        self.app_token = app_token
        self.clock = clock
        self._socket_factory = socket_factory
        # Decision / ignored-click lines go to stdout (container logs) for audit.
        self._echo = echo or (lambda msg: print(msg, flush=True))
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self._socket: Any = None
        self.slack_status: str = "connecting"  # connecting | connected | error
        self.slack_detail: str = ""

    async def _attach_socket_mode(self) -> None:
        """Build bolt handler on the running loop. Must not run at import time."""
        if self._socket_factory is not None:
            self._socket = self._socket_factory()
            return
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        app = AsyncApp(token=self.bot_token)

        @app.action(APPROVE_ACTION)
        async def on_approve(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
            await ack()
            await self.handle_action(
                request_id=str(action.get("value", "")),
                user_id=str(body.get("user", {}).get("id", "")),
                approved=True,
                channel=str(body.get("channel", {}).get("id", self.channel_id)),
                message_ts=str(body.get("message", {}).get("ts", "")),
            )

        @app.action(DENY_ACTION)
        async def on_deny(ack: Any, body: dict[str, Any], action: dict[str, Any]) -> None:
            await ack()
            await self.handle_action(
                request_id=str(action.get("value", "")),
                user_id=str(body.get("user", {}).get("id", "")),
                approved=False,
                channel=str(body.get("channel", {}).get("id", self.channel_id)),
                message_ts=str(body.get("message", {}).get("ts", "")),
            )

        # AsyncSocketModeHandler constructs aiohttp.ClientSession → needs running loop.
        self._socket = AsyncSocketModeHandler(app, self.app_token)

    def is_socket_connected(self) -> bool:
        sock = self._socket
        if sock is None:
            return False
        if isinstance(sock, StubSocketMode):
            return sock.is_connected
        client = getattr(sock, "client", None)
        probe = getattr(client, "is_connected", None) if client is not None else None
        # slack_sdk's aiohttp SocketModeClient.is_connected is a coroutine function; calling it
        # without await returned a truthy coroutine object. Use the tracked status instead.
        if probe is not None and not inspect.iscoroutinefunction(probe):
            return bool(probe())
        return self.slack_status == "connected"

    async def start(self) -> None:
        """Attach Socket Mode (if needed) then connect. Call only from a running loop."""
        from bourse.approval.redact import redact_secrets

        self.slack_status = "connecting"
        self.slack_detail = ""
        try:
            if self._socket is None:
                await self._attach_socket_mode()
            assert self._socket is not None
            await self._socket.connect_async()
            self.slack_status = "connected"
        except Exception as exc:
            from bourse.approval.redact import classify_slack_connect_error

            klass = classify_slack_connect_error(exc)
            self.slack_status = "error"
            # detail starts with the class so stack-up prints e.g. dns_error / invalid_auth
            self.slack_detail = f"{klass}: {redact_secrets(f'{type(exc).__name__}: {exc}')}"
            log.error("slack socket mode failed: %s", self.slack_detail)
            raise

    async def stop(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close_async()
            except Exception as exc:
                from bourse.approval.redact import redact_secrets

                log.warning("slack socket close: %s", redact_secrets(str(exc)))
            self._socket = None

    async def handle_action(
        self,
        *,
        request_id: str,
        user_id: str,
        approved: bool,
        channel: str = "",
        message_ts: str = "",
    ) -> None:
        """Process a button click (real Socket Mode or test injection)."""
        action = "approve" if approved else "deny"
        async with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                # A click only counts if its request_id is the pending request. Older or
                # settled messages, or requests owned by another process, never resolve it.
                self._echo(
                    f"slack click ignored action={action} request_id={request_id} "
                    f"user={user_id} message_ts={message_ts or '-'} reason=expired_or_unknown"
                )
                try:
                    await self.client.chat_post_ephemeral(
                        channel=channel or self.channel_id,
                        user=user_id,
                        text=(
                            f"expired: request_id {request_id} is not pending; "
                            "this click was ignored"
                        ),
                    )
                except SlackTransportError as exc:
                    log.warning("slack ephemeral failed: %s", exc)
                return
            if user_id not in self.approver_ids:
                self._echo(
                    f"slack click ignored action={action} request_id={request_id} "
                    f"user={user_id} reason=unauthorized"
                )
                try:
                    await self.client.chat_post_ephemeral(
                        channel=channel or pending.channel or self.channel_id,
                        user=user_id,
                        text="not authorized",
                    )
                except SlackTransportError as exc:
                    log.warning("slack ephemeral failed: %s", exc)
                return
            if pending.decision is not None:
                # Idempotent: first valid click already won.
                log.info("slack duplicate click ignored request_id=%s user=%s", request_id, user_id)
                return
            now = self.clock()
            reason = "operator approved" if approved else "operator denied"
            pending.decision = ApprovalDecision(
                approved=approved,
                by=user_id,
                at=now,
                reason=reason,
                request_id=request_id,
                action=action,
            )
            pending.event.set()
            self._echo(
                f"slack decision action={action} request_id={request_id} user={user_id} "
                f"message_ts={message_ts or pending.ts} at={now.isoformat()}"
            )
            try:
                status = "APPROVED" if approved else "DENIED"
                await self.client.chat_update(
                    channel=channel or pending.channel,
                    ts=message_ts or pending.ts,
                    text=(
                        f"{format_summary(pending.req.trigger, pending.req.facts)} "
                        f"-> {status} by {user_id}"
                    ),
                    blocks=[
                        *build_blocks(pending.req, with_buttons=False),
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": (f"*{status}* by `{user_id}` at `{now.isoformat()}`"),
                                }
                            ],
                        },
                    ],
                )
            except SlackTransportError as exc:
                log.warning("slack update after decision failed: %s", exc)

    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        notify = req.trigger in NOTIFY_TRIGGERS
        with_buttons = wants_buttons(req.trigger)
        try:
            posted = await self.client.chat_post_message(
                channel=self.channel_id,
                text=build_fallback_text(req),
                blocks=build_blocks(req, with_buttons=with_buttons),
            )
        except SlackTransportError as exc:
            log.error("slack unreachable for request_id=%s: %s", req.request_id, exc)
            return ApprovalDecision(
                approved=False,
                by="slack",
                at=self.clock(),
                reason="slack_unreachable",
                request_id=req.request_id,
                action="unreachable",
            )
        if notify:
            return ApprovalDecision(
                approved=False,
                by="slack-notify",
                at=self.clock(),
                reason="notify",
            )
        event = asyncio.Event()
        pending = _Pending(
            req=req,
            event=event,
            channel=str(posted.get("channel", self.channel_id)),
            ts=str(posted.get("ts", "")),
        )
        async with self._lock:
            self._pending[req.request_id] = pending
        self._echo(
            f"slack request posted trigger={req.trigger.value} request_id={req.request_id} "
            f"subject={req.subject_id} message_ts={pending.ts} timeout_s={timeout_s:.0f}"
        )
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_s)
            except TimeoutError:
                log.info("slack approval timeout request_id=%s", req.request_id)
                now = self.clock()
                try:
                    await self.client.chat_update(
                        channel=pending.channel or self.channel_id,
                        ts=pending.ts,
                        text=(f"{format_summary(req.trigger, req.facts)} -> EXPIRED (timeout)"),
                        blocks=[
                            *build_blocks(req, with_buttons=False),
                            {
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": (f"*EXPIRED* (timeout) at `{now.isoformat()}`"),
                                    }
                                ],
                            },
                        ],
                    )
                except SlackTransportError as exc:
                    log.warning("slack update after timeout failed: %s", exc)
                self._echo(f"slack request expired request_id={req.request_id} reason=timeout")
                return ApprovalDecision(
                    approved=False,
                    by="slack",
                    at=now,
                    reason="timeout",
                    request_id=req.request_id,
                    action="timeout",
                )
            assert pending.decision is not None
            return pending.decision
        finally:
            async with self._lock:
                self._pending.pop(req.request_id, None)
