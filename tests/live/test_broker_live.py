"""Broker end-to-end on the 3 fixtures x 2 repeats through the running stack.

Needs `bourse stack up`, `bourse stack register` and `bourse stack activate --all` first.
Run with `uv run pytest -m live`. Asserts runs complete, ledger rows exist and the resident
peak stays under the limit; grades are printed, not asserted.
"""

import pytest

from bourse.broker.runner import e2e_all, e2e_summary
from bourse.config import get_settings

pytestmark = pytest.mark.live


async def test_e2e_three_fixtures_twice_through_stack() -> None:
    settings = get_settings()
    rows, out, stats = await e2e_all(settings, repeat=2)
    for line in e2e_summary(rows, stats, settings.bourse_resident_limit_bytes):
        print(line)
    print(f"written: {out}")
    for r in rows:
        print(r.dataset, r.repeat, r.run_id, r.status, r.selections)
    assert [r.status for r in rows] == ["SUCCEEDED"] * 6
    assert all(r.tokens_in > 0 and r.tokens_out > 0 for r in rows)
    assert all(r.spotcheck_total >= 5 for r in rows)
    assert all(r.trace_id for r in rows)
    assert stats.resident_peak <= settings.bourse_resident_limit_bytes
