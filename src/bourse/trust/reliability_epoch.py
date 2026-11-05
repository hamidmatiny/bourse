"""Bump reliability epoch on reinstate so counters start fresh (ADR-0048).

Reliability is keyed by (agent_id, skill, card_major, rel_epoch). Ledger rows store
`agent_major = card_major + 10_000 * rel_epoch` so old-epoch s/f never mix into the new
score. Agent tags carry `rel_epoch:N` (bumped on every QUARANTINED→ACTIVE reinstate).
"""

from bourse.ledger.recorder import agent_major

REL_EPOCH_PREFIX = "rel_epoch:"
REL_EPOCH_MAJOR_STRIDE = 10_000


def reliability_epoch(tags: tuple[str, ...]) -> int:
    for tag in tags:
        if tag.startswith(REL_EPOCH_PREFIX):
            try:
                return int(tag[len(REL_EPOCH_PREFIX) :])
            except ValueError:
                return 0
    return 0


def bump_rel_epoch(tags: tuple[str, ...]) -> tuple[str, ...]:
    epoch = reliability_epoch(tags) + 1
    rest = tuple(t for t in tags if not t.startswith(REL_EPOCH_PREFIX))
    return (*rest, f"{REL_EPOCH_PREFIX}{epoch}")


def effective_agent_major(version: str, tags: tuple[str, ...] = ()) -> int:
    return agent_major(version) + REL_EPOCH_MAJOR_STRIDE * reliability_epoch(tags)


def rel_epoch_from_major(agent_major_value: int) -> int:
    """Decode rel_epoch from a ledger/score agent_major."""
    return int(agent_major_value) // REL_EPOCH_MAJOR_STRIDE


def card_major_from_encoded(agent_major_value: int) -> int:
    return int(agent_major_value) % REL_EPOCH_MAJOR_STRIDE
