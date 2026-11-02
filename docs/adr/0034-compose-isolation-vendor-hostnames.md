# 0034 — Compose isolation and vendor hostnames

## Context
In host mode every agent is a local process with full network access and every vendor's keys
on disk, and cards point at 127.0.0.1, which the registry only accepts through
`BOURSE_DEV_HOSTS`.

## Decision
- One image (`Dockerfile`: python:3.11-slim, `uv sync --locked --no-dev`, uid 10001); compose
  services differ only by command. `docker-compose.yml` is generated from
  `bourse/stack/topology.py` (`bourse stack compose-file`); a unit test fails if the committed
  file differs from the generated one.
- Networks: `bourse_internal` (internal: true, subnet 172.30.55.0/24, a static IP per
  container) for everything; `bourse_host` additionally for meter (the only path to
  host.docker.internal:11434), registry, postgres, jaeger and otel-collector, which publish
  127.0.0.1 ports for inspection. Agents publish no ports.
- Vendor hostnames as network aliases: profiler.vendor-a.local, reporter.vendor-a.local,
  pii-precise.vendor-b.local, normalizer.vendor-b.local, pii-fast.vendor-c.local. Cards carry
  these URLs. Registry domain rule: a URL host must equal the vendor domain or be a subdomain
  of it (`host == d or host.endswith("." + d)`); `BOURSE_DEV_HOSTS` is empty in compose.
- Each agent container mounts only `./data/keys/<its vendor>` read-only and gets only its own
  meter key, in a per-service env file (`data/run/env/<service>.env`, mode 0600, written by
  `bourse stack up`, never committed).
- Compose mode is its own deployment: same Postgres container, database `bourse_compose`
  (created by the `migrate` service). Agents therefore register fresh as PENDING and are
  admitted by exam; host mode keeps the `bourse` database and its Phase 2-4 history.
- `bourse stack up --mode compose|host` (compose default); host mode prints
  "WARNING: egress not enforced in host mode" on every start and status. Broker and CLI flows
  run inside compose: `docker compose run --rm broker bourse e2e all`, data/ mounted.
- MCP servers keep DNS-rebinding protection inside compose with `mcp-<name>` as the allowed
  host. The broker samples Ollama `/api/ps` through an authenticated meter passthrough
  (`GET /ollama/api/ps`), because only the meter reaches the host.

## Consequences
- An agent process cannot read another vendor's signing key or reach the internet directly.
- The compose stack needs `docker compose build` first.

## Status
Accepted (Phase 5a).
