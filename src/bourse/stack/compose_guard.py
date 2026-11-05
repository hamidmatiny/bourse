"""Refuse host CLI that must run inside bourse_internal (ADR-0048)."""

from __future__ import annotations

import os
import sys

from bourse.stack.launcher import current_mode

COMPOSE_HINT = "run inside the network: docker compose run --rm broker bourse"


def in_compose_network() -> bool:
    return os.environ.get("BOURSE_IN_COMPOSE", "").strip() in {"1", "true", "yes"}


def refuse_if_compose_host(argv_suffix: str) -> None:
    """If stack mode is compose and we are on the host, exit quickly with the docker hint."""
    if in_compose_network():
        return
    if current_mode() != "compose":
        return
    print(f"{COMPOSE_HINT} {argv_suffix}", file=sys.stderr)
    raise SystemExit(2)
