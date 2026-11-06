"""Bench run preconditions (ADR-0051 fix round 3).

Run `adr0052-b2-6a` started on a stack where nothing was registered. Every cell errored, the
errors were checkpointed and the run exited 0. Before any cell runs, `bench run` (in the
container) and `bench launch` (on the host) now check the registry:
- every honest roster agent is registered and ACTIVE when a config uses the broker (B1-B3) or
  the config has adversary specs;
- every pii_separation agent is registered and ACTIVE.

On failure they exit non-zero and print each problem. `agent_problems` is pure, so it can be
tested without a stack.
"""

from collections.abc import Sequence

import httpx

from bourse.adversaries.catalog import VENDOR as ADVERSARY_VENDOR
from bourse.contracts.bench import BenchConfig
from bourse.contracts.registry import AgentRecord
from bourse.contracts.trust import TrustState


def agent_problems(
    config: BenchConfig, agents: Sequence[AgentRecord], expected_honest: int
) -> list[str]:
    problems: list[str] = []
    by_id = {a.agent_id: a for a in agents}
    honest = [a for a in agents if a.vendor_domain != ADVERSARY_VENDOR]
    if any(c.baseline != "B0" for c in config.configs) or config.adversaries:
        if len(honest) < expected_honest:
            problems.append(
                f"registered honest agents {len(honest)}/{expected_honest} "
                "(run `uv run bourse stack up --mode compose`; it registers and waits for exams)"
            )
        problems += [
            f"{a.agent_id} is {a.state.value}, not ACTIVE"
            for a in sorted(honest, key=lambda x: x.agent_id)
            if a.state is not TrustState.ACTIVE
        ]
    if config.pii_separation is not None:
        for agent_id in config.pii_separation.agents:
            rec = by_id.get(agent_id)
            line = (
                f"pii_separation agent {agent_id} is not registered"
                if rec is None
                else f"{agent_id} is {rec.state.value}, not ACTIVE"
                if rec.state is not TrustState.ACTIVE
                else None
            )
            if line is not None and line not in problems:
                problems.append(line)
    return problems


def fetch_agents(registry_url: str, timeout_s: float = 10.0) -> list[AgentRecord]:
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(f"{registry_url.rstrip('/')}/agents")
        resp.raise_for_status()
        return [AgentRecord.model_validate(a) for a in resp.json()]


def check(config: BenchConfig, registry_url: str, expected_honest: int) -> list[str]:
    """Problems, or [] when the run may start. An unreachable registry is a problem."""
    try:
        agents = fetch_agents(registry_url)
    except (httpx.HTTPError, ValueError) as exc:
        return [f"registry {registry_url} unreachable: {type(exc).__name__}: {exc}"]
    return agent_problems(config, agents, expected_honest)
