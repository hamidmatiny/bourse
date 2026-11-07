"""In-memory per-task budgets registered by the broker (ADR-0041).

Hard cap (ADR-0041 amendment, Phase 6b): a call is admitted only if
`estimated prompt tokens + max_tokens <= max_tokens(task) - used`. Before 6b the prompt was not
counted, so a task could end above its ceiling (ADR-0052 amendment 2: 2 of 10 tasks).
"""

from dataclasses import dataclass, field
from enum import Enum

from bourse.contracts.metering import TaskBudget, TaskBudgetStatus


@dataclass
class _Use:
    tokens: int = 0
    calls: int = 0


class Admission(Enum):
    OK = "ok"
    UNREGISTERED = "unregistered"
    CALLS = "calls"  # max_calls reached
    TOKENS = "tokens"  # estimated prompt + max_tokens > remaining tokens


@dataclass
class BudgetStore:
    """task_id -> (ceiling, usage). Not durable; a meter restart clears ceilings."""

    _ceil: dict[str, TaskBudget] = field(default_factory=dict)
    _use: dict[str, _Use] = field(default_factory=dict)
    # Requests for a task currently inside the meter (gate queue + upstream), any profile.
    _in_flight: dict[str, int] = field(default_factory=dict)
    # Budget refusal codes returned per task (budget_exhausted / task_budget_exceeded).
    _refusals: dict[str, list[str]] = field(default_factory=dict)

    def put(self, budget: TaskBudget) -> None:
        self._ceil[budget.task_id] = budget
        self._use.setdefault(budget.task_id, _Use())

    def get(self, task_id: str) -> TaskBudget | None:
        return self._ceil.get(task_id)

    def remaining(self, task_id: str) -> int | None:
        ceil = self._ceil.get(task_id)
        if ceil is None:
            return None
        use = self._use.get(task_id) or _Use()
        return ceil.max_tokens - use.tokens

    def admit(self, task_id: str, prompt_estimate: int, max_tokens: int) -> Admission:
        ceil = self._ceil.get(task_id)
        use = self._use.get(task_id)
        if ceil is None or use is None:
            return Admission.UNREGISTERED
        if use.calls + 1 > ceil.max_calls:
            return Admission.CALLS
        if prompt_estimate + max_tokens > ceil.max_tokens - use.tokens:
            return Admission.TOKENS
        return Admission.OK

    def record(self, task_id: str, tokens: int) -> None:
        use = self._use.setdefault(task_id, _Use())
        use.calls += 1
        use.tokens += tokens

    def begin(self, task_id: str) -> None:
        self._in_flight[task_id] = self._in_flight.get(task_id, 0) + 1

    def end(self, task_id: str) -> None:
        left = self._in_flight.get(task_id, 0) - 1
        if left > 0:
            self._in_flight[task_id] = left
        else:
            self._in_flight.pop(task_id, None)

    def refuse(self, task_id: str, code: str) -> None:
        self._refusals.setdefault(task_id, []).append(code)

    def status(self, task_id: str) -> TaskBudgetStatus:
        ceil = self._ceil.get(task_id)
        use = self._use.get(task_id) or _Use()
        return TaskBudgetStatus(
            task_id=task_id,
            registered=ceil is not None,
            max_tokens=None if ceil is None else ceil.max_tokens,
            max_calls=None if ceil is None else ceil.max_calls,
            calls=use.calls,
            tokens=use.tokens,
            in_flight=self._in_flight.get(task_id, 0),
            refusals=tuple(self._refusals.get(task_id, ())),
        )
