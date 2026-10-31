"""Broker end-to-end on the 3 fixtures through the running stack.

Needs `bourse stack up`, `bourse stack register` and `bourse stack activate --all` first.
Run with `uv run pytest -m live`. Asserts runs complete; grades are printed, not asserted.
"""

import pytest

from bourse.broker.runner import e2e_all, e2e_summary
from bourse.config import get_settings

pytestmark = pytest.mark.live


async def test_e2e_three_fixtures_through_stack() -> None:
    rows, out = await e2e_all(get_settings())
    for line in e2e_summary(rows):
        print(line)
    print(f"written: {out}")
    for r in rows:
        print(r.dataset, r.run_id, r.status, [(s["subtask"], s["state"]) for s in r.subtasks])
    assert [r.status for r in rows] == ["SUCCEEDED"] * 3
    assert all(r.tokens_in > 0 and r.tokens_out > 0 for r in rows)
