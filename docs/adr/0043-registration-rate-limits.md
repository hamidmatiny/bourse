# 0043 — Registration rate limits

## Context
`POST /agents/register` and refresh are unauthenticated. An attacker can flood the registry
with inline cards and fill the append-only `violations` table.

## Decision
- Token bucket per source IP and per vendor on register and refresh. Default 10/min
  (`BOURSE_REGISTER_RATE_PER_MIN`). Burst equals the rate. Excess -> HTTP 429.
- Unauthenticated (pushed, not fetched) violations: the first N per vendor per UTC hour
  (`BOURSE_INLINE_VIOLATIONS_PER_HOUR`, default 20) are stored as individual `violations`
  rows; further rows increment `violation_aggregates` (vendor_domain, hour, kind, count).
  That table is updatable (not append-only). alembic 0007.

## Consequences
- A single registry process holds the buckets in memory; a restart refills them.
- Search and trust still see the first N individual rows; the counter is for flood evidence.

## Status
Accepted (Phase 5b).
