"""Choose SlackApprover / HttpApprover / CliApprover / DenyApprover (ADR-0048)."""

from __future__ import annotations

import sys
from typing import TextIO

from bourse.approval.base import Approver
from bourse.approval.cli import CliApprover
from bourse.approval.deny import DenyApprover
from bourse.config import Settings

SLACK_ENV_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL_ID",
    "BOURSE_SLACK_APPROVERS",
)


def missing_slack_env(settings: Settings) -> list[str]:
    """Names of Slack env vars that are empty (for operator diagnostics)."""
    missing: list[str] = []
    if not settings.slack_bot_token.get_secret_value():
        missing.append("SLACK_BOT_TOKEN")
    if not settings.slack_app_token.get_secret_value():
        missing.append("SLACK_APP_TOKEN")
    if not settings.slack_channel_id.strip():
        missing.append("SLACK_CHANNEL_ID")
    if not settings.slack_approver_ids:
        missing.append("BOURSE_SLACK_APPROVERS")
    return missing


def select_approver(
    settings: Settings,
    *,
    tty: bool | None = None,
    echo: TextIO | None = None,
    start_socket: bool = False,
    prefer_http: bool = False,
) -> Approver:
    """Slack if env complete; else HttpApprover if URL+key; else Cli on TTY; else Deny.

    Prints the choice. When Slack is incomplete, prints the missing env var names.
    `prefer_http`: use HttpApprover before local Slack (compose broker/registry).
    """
    out = echo if echo is not None else sys.stdout
    missing = missing_slack_env(settings)
    http_url = settings.bourse_approver_url.strip()
    http_key = settings.bourse_approver_key.get_secret_value()
    http_ready = bool(http_url and http_key)

    if prefer_http and http_ready:
        from bourse.approval.http_client import HttpApprover

        print(f"approver=HttpApprover url={http_url}", file=out)
        return HttpApprover(http_url, http_key)

    if settings.slack_env_complete:
        from bourse.approval.slack import BoltSlackClient, SlackApprover

        bot = settings.slack_bot_token.get_secret_value()
        app = settings.slack_app_token.get_secret_value()
        approver: Approver = SlackApprover(
            channel_id=settings.slack_channel_id.strip(),
            approver_ids=settings.slack_approver_ids,
            client=BoltSlackClient(bot),
            bot_token=bot,
            app_token=app,
            start_socket=start_socket,
        )
        print("approver=SlackApprover", file=out)
        return approver

    if missing:
        print(f"approver: Slack not selected; missing {', '.join(missing)}", file=out)

    if http_ready:
        from bourse.approval.http_client import HttpApprover

        print(f"approver=HttpApprover url={http_url}", file=out)
        return HttpApprover(http_url, http_key)

    is_tty = sys.stdin.isatty() if tty is None else tty
    if is_tty:
        print("approver=CliApprover", file=out)
        return CliApprover()
    print("approver=DenyApprover", file=out)
    return DenyApprover()


def select_host_approver(settings: Settings, *, echo: TextIO | None = None) -> Approver:
    """Approver for host CLI commands (`trust reinstate`, `approve test`).

    In compose mode the `approver` service already holds the Slack Socket Mode connection. A
    second connection with the same app token (the host's own SlackApprover) splits click
    delivery between two processes: a click routed to the process that does not hold the
    pending request is dropped. So in compose mode the host always calls the approver service
    over its published port (HttpApprover); only host mode uses a local SlackApprover.
    """
    from bourse.stack.launcher import current_mode, load_secrets

    out = echo if echo is not None else sys.stdout
    if current_mode() != "compose":
        return select_approver(settings, start_socket=True, prefer_http=False, echo=out)
    from bourse.approval.http_client import HttpApprover
    from bourse.stack.topology import APPROVER_PORT

    url = f"http://127.0.0.1:{APPROVER_PORT}"
    key = settings.bourse_approver_key.get_secret_value() or str(
        load_secrets().get("approver_key", "")
    )
    print(
        f"approver=HttpApprover url={url} (compose approver is the only Slack consumer)", file=out
    )
    return HttpApprover(url, key)
