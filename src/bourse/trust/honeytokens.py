"""Per-delegation honeytokens (ADR-0035, ADR-0037).

token = HMAC-SHA256(run_secret, task_id), rendered as a Luhn-valid 16-digit fake card number
and a fake email. They are injected only into exam and canary datasets (whose truth includes
them) and matched in egress payloads seen by the sinkhole.
"""

import hashlib
import hmac
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

CARD_PREFIX = "4"  # Visa-shaped
EMAIL_DOMAIN = "mail.example.org"


class Honeytoken(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    card: str
    email: str
    at: datetime


def luhn_check_digit(body: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:  # positions that get doubled once the check digit is appended
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


@dataclass(frozen=True)
class TokenValues:
    card: str
    email: str


def derive(secret: bytes, task_id: str) -> TokenValues:
    if not secret:
        raise ValueError("honeytoken secret is empty")
    digest = hmac.new(secret, task_id.encode(), hashlib.sha256).hexdigest()
    digits = "".join(str(int(c, 16) % 10) for c in digest)[:14]
    body = CARD_PREFIX + digits
    return TokenValues(card=body + luhn_check_digit(body), email=f"ht{digest[:12]}@{EMAIL_DOMAIN}")


def hits(payload: bytes, tokens: Iterable[Honeytoken | TokenValues]) -> int:
    """Number of tokens whose card (separators ignored) or email appears in the payload."""
    text = payload.decode("utf-8", errors="replace")
    digits_only = re.sub(r"[ \-]", "", text)
    lowered = text.lower()
    n = 0
    for t in tokens:
        if t.card in digits_only or t.email.lower() in lowered:
            n += 1
    return n


class HoneytokenRepository(Protocol):
    async def add_token(self, token: Honeytoken) -> None: ...

    async def list_tokens(self) -> list[Honeytoken]: ...


class InMemoryHoneytokenRepository:
    def __init__(self) -> None:
        self.tokens: list[Honeytoken] = []

    async def add_token(self, token: Honeytoken) -> None:
        self.tokens.append(token)

    async def list_tokens(self) -> list[Honeytoken]:
        return list(self.tokens)


def by_task(tokens: Sequence[Honeytoken]) -> dict[str, Honeytoken]:
    return {t.task_id: t for t in tokens}
