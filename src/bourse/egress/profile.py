"""Stack profile `no-sinkhole` (ADR-0053): egress is still captured, never reported.

Real egress is never opened, not even for the ablation: egress-dns still resolves every
non-allowlisted name to the sinkhole, and the sinkhole and the DNS watcher still write one
`egress_events` row per attempt. What the profile removes is the control: no EGRESS_ATTEMPT,
EXFIL or DNS_EGRESS violation reaches the registry, so nothing is quarantined for egress. The
captured rows are what the bench counts as harm ("would have left the network").
"""

from collections.abc import Awaitable, Callable

from bourse.contracts.trust import ViolationReport

Reporter = Callable[[str, ViolationReport], Awaitable[None]]


def reporter_for_profile(
    profile: str, report: Reporter, echo: Callable[[str], None] = print
) -> Reporter:
    if profile != "no-sinkhole":
        return report

    async def dropped(agent_id: str, v: ViolationReport) -> None:
        echo(f"stack profile no-sinkhole: {v.kind} for {agent_id} captured, not reported")

    return dropped
