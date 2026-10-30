"""Executor protocol (ADR-0009). Implementations land in Phase 6.

LLM-bound work goes through an asyncio semaphore sized OLLAMA_NUM_PARALLEL; CPU-bound work
goes through a ProcessPoolExecutor. No Ray.
"""

from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Protocol, TypeVar

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
