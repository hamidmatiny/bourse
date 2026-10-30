"""Wall-clock deadline around a whole call (ADR-0007).

This is a cancel scope, not an httpx read timeout: a server that drips one byte every
0.5 s never trips a read timeout but is still cancelled here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio


class DeadlineExceeded(TimeoutError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"deadline of {seconds:.3f}s exceeded")
        self.seconds = seconds


@asynccontextmanager
async def deadline(seconds: float) -> AsyncIterator[None]:
    if seconds <= 0:
        raise DeadlineExceeded(seconds)
    with anyio.move_on_after(seconds) as scope:
        yield
    if scope.cancelled_caught:
        raise DeadlineExceeded(seconds)
