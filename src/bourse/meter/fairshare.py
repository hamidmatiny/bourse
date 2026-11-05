"""Meter fair share: per-agent in-flight + global OLLAMA_NUM_PARALLEL (ADR-0042 r2 / ADR-0044).

Bucket capacity == tokens_per_min. A request larger than capacity is rejected immediately
(never queued forever). Queue waits have a hard timeout -> QueueTimeout (CONTENTION).

Waiting for the global upstream semaphore (size = OLLAMA_NUM_PARALLEL) is included in
queue_wait_ms so Ollama-internal slot contention is visible at the meter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class RateLimitUnsatisfiable(Exception):
    """requested tokens exceed bucket capacity; caller must 429, not wait."""


class QueueTimeout(Exception):
    """Waited longer than queue_timeout_s for a slot or token refill."""


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class AgentGate:
    """Serialize upstream LLM calls: global Ollama slots, then per-agent in-flight + tokens."""

    max_in_flight: int = 1
    tokens_per_min: float = 60_000.0
    queue_timeout_s: float = 60.0
    # Matches host OLLAMA_NUM_PARALLEL; meter waits here instead of inside Ollama.
    global_parallel: int = 1
    _sems: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _global: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._global = asyncio.Semaphore(max(1, int(self.global_parallel)))

    @property
    def capacity(self) -> float:
        return self.tokens_per_min

    def _sem(self, agent_id: str) -> asyncio.Semaphore:
        if agent_id not in self._sems:
            self._sems[agent_id] = asyncio.Semaphore(max(1, self.max_in_flight))
        return self._sems[agent_id]

    def _global_sem(self) -> asyncio.Semaphore:
        assert self._global is not None
        return self._global

    async def acquire(self, agent_id: str, requested_tokens: int) -> float:
        """Wait for global slot, in-flight slot, and token budget. Returns queue_wait_ms."""
        need = max(1, requested_tokens)
        if self.tokens_per_min > 0 and need > self.capacity:
            raise RateLimitUnsatisfiable(
                f"requested {need} tokens exceeds bucket capacity {self.capacity:.0f}"
            )
        started = time.monotonic()
        timeout = max(0.001, self.queue_timeout_s)
        global_held = False
        agent_held = False
        try:
            await asyncio.wait_for(self._global_sem().acquire(), timeout=timeout)
            global_held = True
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise QueueTimeout(f"global upstream wait spent budget for {agent_id}")
            await asyncio.wait_for(self._sem(agent_id).acquire(), timeout=remaining)
            agent_held = True
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise QueueTimeout(f"queue budget spent before token wait for {agent_id}")
            await asyncio.wait_for(self._wait_tokens(agent_id, need), timeout=remaining)
        except TimeoutError as exc:
            if agent_held:
                self._sem(agent_id).release()
            if global_held:
                self._global_sem().release()
            raise QueueTimeout(
                f"upstream/fair-share wait exceeded {timeout:.1f}s for {agent_id}"
            ) from exc
        except BaseException:
            if agent_held:
                self._sem(agent_id).release()
            if global_held:
                self._global_sem().release()
            raise
        return (time.monotonic() - started) * 1000

    def release(self, agent_id: str) -> None:
        self._sem(agent_id).release()
        self._global_sem().release()

    async def _wait_tokens(self, agent_id: str, need: int) -> None:
        if self.tokens_per_min <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                bucket = self._buckets.get(agent_id)
                if bucket is None:
                    bucket = _Bucket(tokens=self.capacity, updated=now)
                    self._buckets[agent_id] = bucket
                elapsed = now - bucket.updated
                bucket.tokens = min(
                    self.capacity, bucket.tokens + elapsed * (self.tokens_per_min / 60.0)
                )
                bucket.updated = now
                if bucket.tokens >= need:
                    bucket.tokens -= need
                    return
                deficit = need - bucket.tokens
                wait_s = deficit / (self.tokens_per_min / 60.0)
            await asyncio.sleep(min(wait_s, 0.05))
