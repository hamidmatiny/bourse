"""Fail fast when the Postgres in BOURSE_DATABASE_URL is not reachable.

The host and port come from the URL (local default 127.0.0.1:55432; CI service 5432), never a
hard-coded port.
"""

import socket

import pytest
from sqlalchemy.engine import make_url

from bourse.config import get_settings


@pytest.fixture(scope="session", autouse=True)
def postgres_reachable() -> None:
    url = make_url(get_settings().bourse_database_url)
    host, port = url.host or "127.0.0.1", url.port or 5432
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return
    except OSError:
        pytest.exit(
            f"postgres not reachable on {host}:{port} (BOURSE_DATABASE_URL); "
            "run docker compose up -d postgres",
            returncode=1,
        )
