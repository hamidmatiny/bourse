"""In-memory per-task budgets registered by the broker (ADR-0041)."""

from dataclasses import dataclass, field

from bourse.contracts.metering import TaskBudget


@dataclass
class _Use:
    tokens: int = 0
    calls: int = 0


@dataclass
class BudgetStore:
    """task_id -> (ceiling, usage). Not durable; a meter restart clears ceilings."""

    _ceil: dict[str, TaskBudget] = field(default_factory=dict)
    _use: dict[str, _Use] = field(default_factory=dict)

    def put(self, budget: TaskBudget) -> None:
        self._ceil[budget.task_id] = budget
        self._use.setdefault(budget.task_id, _Use())

    def get(self, task_id: str) -> TaskBudget | None:
        return self._ceil.get(task_id)

    def would_exceed(self, task_id: str, requested_tokens: int) -> bool:
        ceil = self._ceil.get(task_id)
        use = self._use.get(task_id)
        if ceil is None or use is None:
            return True
        if use.calls + 1 > ceil.max_calls:
            return True
        return use.tokens + requested_tokens > ceil.max_tokens

    def record(self, task_id: str, tokens: int) -> None:
        use = self._use.setdefault(task_id, _Use())
        use.calls += 1
        use.tokens += tokens
