"""Never log or echo Slack token values (ADR-0048)."""

from __future__ import annotations

import re

from bourse.config import Settings

# Bot/user tokens (xoxb-/xoxp-/…) and app-level Socket Mode tokens (xapp-).
_TOKEN_RE = re.compile(r"(?:xox[a-z]|xapp)-[A-Za-z0-9-]+", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Replace Slack token-shaped substrings with [REDACTED]."""
    return _TOKEN_RE.sub("[REDACTED]", text)


def classify_slack_connect_error(exc: BaseException) -> str:
    """Stable error class for /healthz detail (never includes token values)."""
    name = type(exc).__name__
    text = redact_secrets(f"{name}: {exc}").lower()
    if (
        "dns" in name.lower()
        or "gaierror" in text
        or "name resolution" in text
        or "nodename nor servname" in text
        or "name or service not known" in text
        or "temporary failure in name resolution" in text
    ):
        return "dns_error"
    if "invalid_auth" in text or "not_authed" in text or "token_revoked" in text:
        return "invalid_auth"
    if "timeout" in name.lower() or "timed out" in text or "timeout" in text:
        return "timeout"
    if "ssl" in name.lower() or "certificate" in text or "tls" in text:
        return "tls_error"
    if "connection refused" in text or "connector" in name.lower():
        return "connect_error"
    return "connect_error"


def slack_env_presence(settings: Settings) -> dict[str, str]:
    """Map env names → 'set' | 'missing'. Never includes values."""
    return {
        "SLACK_BOT_TOKEN": ("set" if settings.slack_bot_token.get_secret_value() else "missing"),
        "SLACK_APP_TOKEN": ("set" if settings.slack_app_token.get_secret_value() else "missing"),
        "SLACK_CHANNEL_ID": "set" if settings.slack_channel_id.strip() else "missing",
        "BOURSE_SLACK_APPROVERS": ("set" if settings.slack_approver_ids else "missing"),
    }


def format_slack_env_presence(settings: Settings) -> str:
    return ", ".join(f"{k}={v}" for k, v in slack_env_presence(settings).items())
