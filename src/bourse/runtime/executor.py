"""Executor protocol (ADR-0009). The full implementation lands in Phase 6.

LLM-bound work goes through an asyncio semaphore sized OLLAMA_NUM_PARALLEL; CPU-bound work
goes through a ProcessPoolExecutor. No Ray.
"""

from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Protocol, TypeVar

import anyio

A = TypeVar("A")
R = TypeVar("R")


class WorkKind(StrEnum):
    LLM = "llm"  # coroutine functions, bounded by the LLM semaphore
    CPU = "cpu"  # picklable sync functions, run in the process pool


class Executor(Protocol):
    async def submit_llm(self, fn: Callable[[A], Awaitable[R]], arg: A) -> R: ...

    async def submit_cpu(self, fn: Callable[[A], R], arg: A) -> R: ...

    async def map(
        self, kind: WorkKind, fn: Callable[[A], R] | Callable[[A], Awaitable[R]], args: Sequence[A]
    ) -> list[R]: ...

    async def aclose(self) -> None: ...


class LlmOnlyExecutor:
    """Phase 3 subset of the Executor protocol: submit_llm behind an anyio semaphore.

    The broker only needs LLM-bound concurrency (each delegation waits on a remote agent's
    LLM). The CPU pool and map() arrive with the Phase 6 executor.
    """

    def __init__(self, llm_slots: int) -> None:
        if llm_slots < 1:
            raise ValueError("llm_slots must be >= 1")
        self._sem = anyio.Semaphore(llm_slots)

    async def submit_llm(self, fn: Callable[[A], Awaitable[R]], arg: A) -> R:
        async with self._sem:
            return await fn(arg)

    async def submit_cpu(self, fn: Callable[[A], R], arg: A) -> R:
        raise NotImplementedError("CPU pool arrives in Phase 6")

    async def map(
        self, kind: WorkKind, fn: Callable[[A], R] | Callable[[A], Awaitable[R]], args: Sequence[A]
    ) -> list[R]:
        raise NotImplementedError("map arrives in Phase 6")

    async def aclose(self) -> None:
        return None
