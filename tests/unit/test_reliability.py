import pytest

from bourse.broker.reliability import ewma, scores, stats_by_agent, wilson_lower_bound
from bourse.contracts.broker import DelegationState
from bourse.contracts.skills import SkillId
from tests.ledger_helpers import history, row


@pytest.mark.parametrize(
    ("s", "f", "expected"),
    [
        (0, 0, 0.0),
        (1, 0, 0.20654931437723742),
        (5, 0, 0.5655175352168252),
        (9, 1, 0.5958499732047616),
        (50, 50, 0.4038315303659957),
    ],
)
def test_wilson_lower_bound_exact(s: int, f: int, expected: float) -> None:
    assert wilson_lower_bound(s, f) == pytest.approx(expected, abs=1e-12)


def test_ewma_oldest_first() -> None:
    assert ewma([], 0.3) is None
    assert ewma([10.0], 0.3) == 10.0
    # 10 -> 0.3*20 + 0.7*10 = 13 -> 0.3*40 + 0.7*13 = 21.1
    assert ewma([10.0, 20.0, 40.0], 0.3) == pytest.approx(21.1)


def test_success_needs_completed_and_spot_check() -> None:
    rows = [
        row("a", ok=True, i=0),
        row("a", state=DelegationState.FAILED_SPOTCHECK, i=1),
        row("a", state=DelegationState.CARD_DRIFT, i=2),
        row("a", state=DelegationState.TIMEOUT, i=3),
    ]
    st = stats_by_agent(rows, SkillId.PII_DETECT, {"a": 1}, 0.3)["a"]
    assert (st.s, st.f) == (1, 3)


def test_major_bump_starts_fresh_counter() -> None:
    rows = history("a", 9, 1, major=1)
    st_old = stats_by_agent(rows, SkillId.PII_DETECT, {"a": 1}, 0.3)["a"]
    st_new = stats_by_agent(rows, SkillId.PII_DETECT, {"a": 2}, 0.3)["a"]
    assert (st_old.s, st_old.f) == (9, 1)
    assert (st_new.s, st_new.f, st_new.wilson_lb) == (0, 0, 0.0)
    assert st_new.ewma_usd is None


def test_other_skills_and_agents_ignored() -> None:
    rows = history("a", 3, 0) + history("a", 2, 0, skill=SkillId.STATS_COMPUTE) + history("b", 1, 1)
    stats = stats_by_agent(rows, SkillId.PII_DETECT, {"a": 1}, 0.3)
    assert list(stats) == ["a"]
    assert (stats["a"].s, stats["a"].f) == (3, 0)


def test_scores_grouped_by_agent_skill_major() -> None:
    rows = history("a", 2, 1) + history("a", 1, 0, major=2) + history("b", 0, 2)
    out = {(sc.agent_id, sc.agent_major): (sc.s, sc.f) for sc in scores(rows, 0.3)}
    assert out == {("a", 1): (2, 1), ("a", 2): (1, 0), ("b", 1): (0, 2)}
