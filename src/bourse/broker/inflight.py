"""In-flight A2A tasks, canceled when the agent is quarantined (ADR-0039).

Cancel is best-effort: the task may already have completed (TaskNotCancelableError /
TaskNotFoundError) or the agent may be unreachable. Those must never abort the broker.
"""

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.types import AgentCard, CancelTaskRequest, TaskNotCancelableError, TaskNotFoundError

CancelFn = Callable[[AgentCard, str, httpx.AsyncClient], Coroutine[Any, Any, None]]

log = logging.getLogger(__name__)


@dataclass
class InFlightHandle:
    agent_id: str
    card: AgentCard
    a2a_task_id: str
    http: httpx.AsyncClient


async def cancel_task(card: AgentCard, a2a_task_id: str, http: httpx.AsyncClient) -> None:
    client = await create_client(card, ClientConfig(streaming=False, httpx_client=http))
    await client.cancel_task(CancelTaskRequest(id=a2a_task_id))


class InFlight:
    def __init__(self, cancel: CancelFn = cancel_task) -> None:
        self._cancel = cancel
        self._by_agent: dict[str, list[InFlightHandle]] = {}

    def add(self, handle: InFlightHandle) -> None:
        self._by_agent.setdefault(handle.agent_id, []).append(handle)

    def remove(self, handle: InFlightHandle) -> None:
        held = self._by_agent.get(handle.agent_id)
        if held is None:
            return
        self._by_agent[handle.agent_id] = [h for h in held if h.a2a_task_id != handle.a2a_task_id]
        if not self._by_agent[handle.agent_id]:
            del self._by_agent[handle.agent_id]

    async def cancel_handle(self, handle: InFlightHandle) -> None:
        self.remove(handle)
        await self._best_effort_cancel(handle)

    async def cancel_agent(self, agent_id: str) -> int:
        handles = list(self._by_agent.pop(agent_id, []))
        canceled = 0
        for handle in handles:
            if await self._best_effort_cancel(handle):
                canceled += 1
        return canceled

    async def _best_effort_cancel(self, handle: InFlightHandle) -> bool:
        try:
            await self._cancel(handle.card, handle.a2a_task_id, handle.http)
            return True
        except (TaskNotCancelableError, TaskNotFoundError) as exc:
            log.warning(
                "cancel best-effort task_id=%s agent_id=%s: %s",
                handle.a2a_task_id,
                handle.agent_id,
                exc,
            )
            return False
        except httpx.HTTPError as exc:
            log.warning(
                "cancel best-effort task_id=%s agent_id=%s: %s",
                handle.a2a_task_id,
                handle.agent_id,
                exc,
            )
            return False
        except Exception as exc:  # transport / JSON-RPC wrappers
            log.warning(
                "cancel best-effort task_id=%s agent_id=%s: %s",
                handle.a2a_task_id,
                handle.agent_id,
                exc,
            )
            return False
