"""Typed task outcomes (ADR-0022). Messages never include input free text."""


class TaskRejected(Exception):
    """The request is not acceptable: REJECTED, no work done."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class TaskFailed(Exception):
    """Work started and could not finish: FAILED."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
