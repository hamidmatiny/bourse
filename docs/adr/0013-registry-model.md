# 0013 — Registry model: immutable revisions, semver rules, rejection taxonomy, state machine

## Context
Vendors publish cards; attackers try forgery (A1), impersonation (A1b), tampering (A2), key
substitution (A3) and rollback/silent mutation (A9).

## Decision
- Identity: `agent_id = slug(card.name) + "@" + vendor_domain`.
- Check order for every card: size (64 KiB) → parse (unknown fields rejected) → revoked? →
  signature against the vendor's pinned JWKS → skills ⊆ `SkillId` → every
  `supportedInterfaces[].url` and `provider.url` host equals the vendor domain (or a configured
  dev host) → semver → version history. Signature is checked first so an edited card is always
  TAMPERED.
- Versions (SemVer 2.0.0 precedence; build metadata ignored): new > current → accept and store a
  revision; new == current with the same payload digest → accepted, `unchanged=true`, nothing
  stored; new == current with a different digest → VERSION_REUSE; new < current →
  VERSION_ROLLBACK. Digest = SHA-256 of the signing payload (signatures excluded).
- Revisions are immutable: unique (agent_id, version) and an append-only trigger in Postgres.
- RejectReason: FORGED_SIGNATURE, TAMPERED, UNTRUSTED_JKU, UNSUPPORTED_ALG, UNSIGNED,
  MALFORMED_CARD, UNKNOWN_SKILL, DOMAIN_MISMATCH, VERSION_ROLLBACK, VERSION_REUSE, CARD_TOO_LARGE,
  FETCH_FAILED, UNKNOWN_VENDOR, UNKNOWN_AGENT, AGENT_REVOKED.
  HARD: FORGED_SIGNATURE, TAMPERED, UNTRUSTED_JKU, UNSUPPORTED_ALG, DOMAIN_MISMATCH,
  VERSION_ROLLBACK, VERSION_REUSE. All others SOFT. Every rejection with a known agent_id writes a
  Violation.
- State machine: PENDING→ACTIVE|REVOKED; ACTIVE→QUARANTINED|REVOKED; QUARANTINED→ACTIVE (requires
  an `ApprovalDecision` with approved=true)|REVOKED; REVOKED is terminal. Illegal transitions
  raise; each transition writes an audit row. New agents start PENDING (admission exam: Phase 5).
- Attribution (narrows the Phase 1 brief, see below): a HARD violation moves an ACTIVE agent to
  QUARANTINED only when the card was fetched by the registry from the vendor's own host (`refresh`,
  or `register` with `card_url`). Inline cards pushed to `POST /agents/register` can come from
  anyone, including an attacker replaying an old signed card or submitting a forgery, so their
  violations are recorded but do not change state. Without this rule any client could quarantine
  any honest agent. PENDING agents are not quarantined (no legal transition); their violations
  are recorded for the Phase 5 admission exam.
- Fetching: only `<scheme>://<vendor host>/.well-known/agent-card.json`, host checked before any
  request (no SSRF to other hosts), no redirects, 5 s wall-clock deadline, 64 KiB cap enforced
  while streaming, content type `application/json` or `application/a2a+json`.

## Consequences
- Rollback by replay is always rejected; it quarantines only when served from the vendor's host.
- Renaming an agent creates a new agent_id.

## Status
Accepted (Phase 1).
