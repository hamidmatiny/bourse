"""Every agent test runs with outbound network blocked: only loopback connects are allowed."""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class ExternalNetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    attempts: list[Any] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(address: Any) -> None:
        if isinstance(address, tuple) and address and address[0] not in LOOPBACK:
            attempts.append(address)
            raise ExternalNetworkBlocked(f"external connection blocked: {address!r}")

    def connect(self: socket.socket, address: Any) -> None:
        check(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield attempts
    assert attempts == [], f"tests attempted external connections: {attempts}"
