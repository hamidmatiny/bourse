"""Executor protocol and pools (ADR-0009; PoolExecutor Phase 6a, ADR-0051).

LLM-bound work goes through an asyncio semaphore sized OLLAMA_NUM_PARALLEL; CPU-bound work
goes through a ProcessPoolExecutor. No Ray.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from enum import StrEnum
from typing import Protocol, TypeVar, cast

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


class PoolExecutor:
    """ADR-0009 executor: LLM-bound coroutines behind a semaphore of `llm_slots`
    (= OLLAMA_NUM_PARALLEL by default), CPU-bound picklable functions in a process pool.

    map() keeps input order. The meter's global gate still bounds real LLM concurrency; the
    semaphore only bounds how many subtasks this process has in flight.
    """

    def __init__(self, llm_slots: int, cpu_workers: int = 2) -> None:
        if llm_slots < 1:
            raise ValueError("llm_slots must be >= 1")
        if cpu_workers < 1:
            raise ValueError("cpu_workers must be >= 1")
        self.llm_slots = llm_slots
        self._sem = anyio.Semaphore(llm_slots)
        self._cpu_workers = cpu_workers
        self._pool: ProcessPoolExecutor | None = None

    async def submit_llm(self, fn: Callable[[A], Awaitable[R]], arg: A) -> R:
        async with self._sem:
            return await fn(arg)

    async def submit_cpu(self, fn: Callable[[A], R], arg: A) -> R:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self._cpu_workers)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, arg)

    async def map(
        self, kind: WorkKind, fn: Callable[[A], R] | Callable[[A], Awaitable[R]], args: Sequence[A]
    ) -> list[R]:
        if kind is WorkKind.LLM and not inspect.iscoroutinefunction(fn):
            raise TypeError("WorkKind.LLM needs a coroutine function")
        results: list[R | None] = [None] * len(args)

        async def one(i: int, arg: A) -> None:
            if kind is WorkKind.LLM:
                results[i] = await self.submit_llm(cast(Callable[[A], Awaitable[R]], fn), arg)
            else:
                results[i] = await self.submit_cpu(cast(Callable[[A], R], fn), arg)

        async with anyio.create_task_group() as tg:
            for i, arg in enumerate(args):
                tg.start_soon(one, i, arg)
        return cast(list[R], results)

    async def aclose(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
