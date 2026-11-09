# 0048 — Slack approver and reinstatement

## Context
Phase 0 defined the Approver protocol and four triggers (ADR-0010). Phase 5b quarantines
via the trust engine but QUARANTINED → ACTIVE still needs a human. Unattended CI must deny;
operators with Slack need structured Approve/Deny that never embeds Agent Card free text or
agent-produced text into the prompt surface.

`bourse_internal` is `internal: true` (no internet). Broker and registry therefore cannot open
Slack Socket Mode. Agent hostnames only resolve inside the compose network, so host-side
`attacks phase5` / exam calls that fetch cards fail with `card_fetch_failed`.

An earlier draft pointed the approver at egress-dns with Slack domain allowlist forwards to
`8.8.8.8`. That cannot work: egress-dns lives only on `bourse_internal` (`internal: true`),
so Docker DNS there has no upstream path for public names. Socket Mode also needs
`wss-primary.slack.com` / `wss-backup.slack.com` (and related hosts), not only `slack.com`.

## Decision
- Compose service `approver` joins **both** `bourse_host` and `bourse_internal`. It is the only
  trust-path service that opens Slack Socket Mode. Slack tokens live only in `approver`'s env
  file.
- **DNS / egress for approver:** the approver uses **Docker default DNS** on `bourse_host`
  (no `dns:` override to egress-dns). Trust boundary: the approver is first-party BOURSE code; it
  only calls Slack Web API + Socket Mode. Network isolation (egress-dns + sinkhole) remains the
  control for untrusted agents/adversaries/MCP on `bourse_internal`. Future option: an
  allowlisting forward HTTP/HTTPS proxy for Slack destinations if we need a network-enforced
  allowlist without relying on egress-dns public forwards.
- `bourse_host` membership is closed: only `approver`, `meter` (Ollama via
  `host.docker.internal`), and published-port tooling (`registry`, `postgres`, `jaeger`,
  `otel-collector`). No agent, adversary, or MCP service may attach to `bourse_host`
  (`HOST_NET_SERVICES` + topology unit test).
- Internal components call the approver over HTTP on `bourse_internal`
  (`BOURSE_APPROVER_URL=http://approver:55310` + `BOURSE_APPROVER_KEY`). `HttpApprover` implements
  the same `Approver` protocol. `ABORT_NOTICE` (broker) and `HIGH_REP_VIOLATION` (registry
  TrustEngine) go through it.
- `SlackApprover` (slack-bolt 1.30.0, Socket Mode) still implements `Approver.request` inside the
  approver service (and on the host for `bourse approve test` when Slack env is complete).
  Messages are built only from `ApprovalFacts`. Approve/Deny; only `BOURSE_SLACK_APPROVERS`;
  first valid click wins; timeout → `timeout`; Slack down → `slack_unreachable`.
- Approver process entry (`bourse approver serve` → `approval.service.serve`) builds
  `AsyncApp` / `AsyncSocketModeHandler` **inside** `async def run_approver()` and runs via
  `asyncio.run`. HTTP (uvicorn) and Socket Mode share that loop. No Slack/aiohttp clients at
  import time. `/healthz` returns `{"slack": "connected"|"connecting"|"error", "detail": …}`
  with HTTP 200 only when `slack == connected`. On connect failure `detail` starts with a stable
  class (`dns_error`, `invalid_auth`, `timeout`, `tls_error`, `connect_error`) so stack-up can
  print the reason. Token values never logged — only `set`/`missing`.
- `select_approver` prints the choice; when Slack is incomplete it prints the **names** of missing
  env vars. Prefer order: local Slack (if complete) → HttpApprover (URL+key) → CliApprover (TTY) →
  DenyApprover. `bourse approve test --require-slack` exits 1 unless `SlackApprover` is selected.
- Host CLI that needs agent DNS refuses in &lt; 2s when `data/run/mode` is `compose` and
  `BOURSE_IN_COMPOSE` is unset: `run inside the network: docker compose run --rm broker bourse …`.
- Reinstatement from the host (no honeytoken secret required on the host):
  1. `POST /agents/{id}/reinstate-exam` — registry runs the exam in-network (same bank as admission).
  2. Poll `GET /agents/{id}/reinstate-exam-result`.
  3. Host requests `REINSTATE_AGENT` via SlackApprover / HttpApprover.
  4. `POST /agents/{id}/reinstate` with the `ApprovalDecision`; registry applies ACTIVE + `rel_epoch`
     bump, or audits stay-QUARANTINED on deny / failed exam. REVOKED never reinstatable.
- The `rel_epoch` bump also resets SOFT strikes: violations are stamped with the epoch at insert
  and the trust engine counts only the current epoch (ADR-0039 amendment, alembic 0011).
  Probation on reinstatement is a possible future policy, not implemented.

- **One Socket Mode consumer (Phase 5c amendment).** In compose mode the host CLI
  (`bourse trust reinstate`, `bourse approve test`) never opens its own Socket Mode connection; it
  calls the `approver` service through its published port (`HttpApprover`,
  127.0.0.1:55310). Two connections with one app token split interactive deliveries between
  processes, and a click delivered to the process without the pending request was dropped.
  Host mode (no compose) still uses a local SlackApprover.
- **Request binding.** A click counts only if its button value equals a pending request_id in
  the receiving process. Any other click (older, settled, expired or unknown request) is
  ignored and answered with an ephemeral `expired: request_id ... is not pending`.
  `ApprovalDecision` carries `request_id` and `action` (approve | deny | timeout |
  unreachable); both are persisted in `state_transitions.approval`. The approver prints
  `slack request posted`, `slack decision`, `slack click ignored` and `slack request expired`
  lines with request_id and message ts.
- **Message.** Action, agent and request_id are first; buttons read `APPROVE <id8>` (primary)
  and `Deny <id8>` (danger, with a confirm dialog). The host prints a banner with the full
  request_id and the expected click before posting.
- A new reinstate exam clears the previous reinstate result, so the host never reads a stale
  exam outcome.

## Consequences
- CI and non-TTY runs never wait on a human.
- Slack messages cannot launder card injection into an operator surface.
- Compose adversaries / exams stay inside the network; host operators still click Slack on the
  laptop (or via the published approver port).
- Approver outbound reach is not network-allowlisted; enforcement is code + closed `bourse_host`
  membership. A forward proxy remains a future hardening option.
- A turned sleeper (honest prefix already consumed) fails reinstate exam **0/5** unless
  `/bourse/reset-behavior` is called first. That 0/5 is a real catch ("reinstatement exam caught
  turned sleeper"), not a harness bug. Acceptance resets behavior before Deny/Approve path tests.
- Slack approval timeout updates the message to *EXPIRED* (`reason=timeout`).
- Phase5 precondition failures (`AGENT_REVOKED` / not ACTIVE before the scenario) are status
  `error (precondition)`, never `missed`. Re-catch after reinstate uses `--reuse-agent` (no
  re-registration).

## Observed: the Deny of 2026-11-05 (`approved=False by=U0CELEXF57H reason=operator denied`)
`reason=operator denied` is produced only by the Deny handler in the process holding the
pending request whose request_id equals the button value, for a user in
`BOURSE_SLACK_APPROVERS` (which held only U0CELEXF57H). request_ids are uuid4, so an older
message's button cannot match. The request_id and message ts of that click were not recorded:
the host did not print the request_id, decisions did not carry it, and `stack down` removed
the database volume and the approver container. The amendment above records both from now on.

## Observed (Phase 5c acceptance)
- A5a sole-provider: caught (SPOTCHECK), exposure 6, first_bad 4, detect_delay 2, strikes 3, harmed 0.
  *Definition note (Phase 7):* these numbers count real deliveries only, the exposure definition
  before ADR-0042 fix round 4. The acceptance log of this catch (`data/logs/acceptance5c/a5a.log`;
  `data/logs/attacks5c-a5a.log` from earlier the same day prints the same) uses the current
  definition, which counts canaries:
  `exposure=8 (real 6, canaries 2) first_bad=4 detect_delay=4 strikes=3`. The two sets of numbers
  match the same catch under the two definitions; they are not evidence of different runs. first_bad is 4 under both definitions, so no canary came before it. Dropping
  the 2 canaries that came after it gives exposure 6 and detect_delay 4 - 2 = 2. No log on disk
  prints 6/4/2 itself. RESULTS.md section g reports the current-definition numbers and this
  projection.
- Reinstate without reset-behavior after that catch: `passed=False exam=0/5` — recorded as
  **reinstatement exam caught turned sleeper 0/5**.
- With `/bourse/reset-behavior` before reinstate: Deny → stays QUARANTINED (audit + approver id);
  Approve → ACTIVE + `rel_epoch` bump (`0 -> 1`, persisted on agent tags; ledger major =
  `card_major + 10000 * rel_epoch` so old-epoch s/f do not mix). `--reuse-agent` skips revoking the
  reused identity at baseline and scenario start (print `reuse-agent … state=ACTIVE`).
- Resume path: `bash scripts/acceptance_5c.sh --from reinstate` (skip unit/db/build/exams).

## Status
Accepted (Phase 5c). Observed section: definition note added in Phase 7.
