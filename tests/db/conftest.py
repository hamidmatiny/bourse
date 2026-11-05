"""Fail fast when Postgres is not published on 127.0.0.1:55432."""

import socket

import pytest


@pytest.fixture(scope="session", autouse=True)
def postgres_on_55432() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=2.0):
            return
    except OSError:
        pytest.exit(
            "postgres not reachable on 55432; run docker compose up -d postgres",
            returncode=1,
        )
