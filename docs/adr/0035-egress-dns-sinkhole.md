# 0035 — Egress DNS sinkhole and attribution

## Context
`internal: true` networks block routing out, but a compromised agent that tries to reach the
internet should be seen and attributed, not just fail silently.

## Decision
- `egress-dns` (dnsmasq in the BOURSE image) is the upstream resolver of agent, MCP, broker
  and probe containers. Docker's embedded DNS answers service names and aliases; every other
  name is forwarded to egress-dns, which forwards allowlisted names to 127.0.0.11 and answers
  everything else with the sinkhole IP. The allowlist is generated from the topology service
  list (unit-tested).
- `sinkhole` listens on 80 and 443, reads at most 4 KiB, answers HTTP with 403 and closes TLS
  without a handshake. Each connection is one append-only `egress_events` row (alembic 0006):
  src_ip, identity from the static IP map (agent_id for agents, service name otherwise), Host
  header or TLS SNI, port, bytes, sha256 of the bytes, honeytoken hits.
- An event from an agent IP is posted to the registry as a HARD `EGRESS_ATTEMPT` violation;
  a payload containing a honeytoken adds a HARD `EXFIL` violation. The evidence is observed
  on the network from a static IP owned by that agent's container, so it counts as attributed
  (the ADR-0013 rule that only attributable evidence changes trust). Quarantine automation is
  Phase 5b; 5a records violations.
- `egress-probe` (profile "test") runs `curl http://attacker.example` and
  `curl https://attacker.example`; both must yield attributed events.

## Consequences
- Parsers are unit-tested on a real ClientHello generated in memory and on an HTTP request
  captured as bytes.
- Egress by IP literal bypasses DNS; it is blocked by the internal network but not recorded.

## Status
Accepted (Phase 5a).
