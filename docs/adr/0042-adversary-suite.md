# 0042 — Adversary suite, scenario methodology, known gaps

## Context
Phase 5a had card-lint attacks A1-A3, A9 in memory and an egress probe. BOURSE-Bench needs
insider-threat agents on the compose network whose catch stage is measured, not assumed.

## Decision
- Malicious vendor `vendor-x.local` is onboarded with a pinned JWKS like any vendor (insider).
- Compose profile `adversaries`: each adversary is a container on `bourse_internal` with alias
  `<name>.vendor-x.local`. Attacker endpoints are `*.attacker.example` (sinkhole only).
- Preflight (required services healthy; DNS from an adversary container resolves
  registry/meter/MCP and sinkholes attacker.example; sinkhole reachable). Failure prints
  `INFRA NOT READY: <check>` and exits non-zero with no results table.
- `caught` means the actual reject/violation reason equals the expected reason: A1
  FORGED_SIGNATURE, A1b DOMAIN_MISMATCH, A2 TAMPERED, A3 UNTRUSTED_JKU, A9
  VERSION_ROLLBACK+VERSION_REUSE, A4a LINT_INJECTION; A4b zero-effect; A5a SPOTCHECK_FAIL;
  A5b CANARY_FAIL; A6a/b EGRESS_ATTEMPT|EXFIL; A6c DNS_EGRESS; A7 SESSION_SMUGGLING; A8a
  budget; A8b deadline; A8c USAGE_MISREPORT. FETCH_FAILED, timeouts, and "exam did not
  finish" are status ERROR, never caught. Summary: caught n/N, missed n/N, error n/N.
  Any error -> exit non-zero. Harmed tasks n/N with N = adversary delegations executed
  (0/0 when none ran). Canary M=3. JSON under `data/attacks/phase5-<utc>.json`.
- A4b is a success when lint accepts it and selection share is unaffected (AgentRecord has no
  description; selection cannot read card text). That is `zero-effect`, not "caught".
- A5b (drop PERSON_NAME after the exam + 3 real tasks) has no deterministic spot check for
  names. Canaries may catch it; if not, the report says so with numbers. Not red.
- Red: crash, honest false quarantine > 0, test failure, or any byte leaving
  `bourse_internal`.

### Known gap: Phase 5a e2e grading vs spot check (0a)

- run_id: `run-20261102T011703Z-076719b5`
- dataset: orders
- skill: `units.normalize` (subtask `unit_norm`)
- agent: `vendor-b-normalizer@vendor-b.local`
- grader: `passed=false` n=1 N=1 (`grade_units` fails when extra conversions are present)
- spot check: `spotcheck_failed 0/5` (`check_units` does not reject extras on columns that
  have no unit-table conversion)
- Graders and thresholds are not changed to hide this. Spot-check coverage of extra
  conversions is a known gap.

### A5b

PERSON_NAME is not in `check_pii` (EMAIL / CARD / SIN only). Catch stage is canary-or-none;
report honestly.

## Consequences
- Honest roster size in compose tests ignores `@vendor-x.local`.
- Phase 1 A1-A3, A9 are re-run through registry `card_url` fetch. Each attack is a Host alias
  (`a1.vendor-x.local`, …) on the `card-attacks` container serving
  `/.well-known/agent-card.json` (fetch_card rejects any other path).

## Fix round 2 (2026-11-02)

- CANARY_FAIL only on COMPLETED + contract-valid + grader-failed canaries
  (`canary_grader_fail`). Timeouts / 429 / cancel / CONTENTION never trust-strike.
- Meter fair share: per-agent max in-flight (default 1) + tokens/min; `queue_wait_ms`
  on each `llm_usage` row. Delegation CONTENTION when queue_wait_ms > 50% of deadline
  (no strike, excluded from reliability f).
- False-quarantine metric is the final snapshot (`honest agents quarantined n/5`) plus
  `quarantine events on honest agents n`. `--reset-honest` reinstates honest agents
  only between separate invocations (printed); never silently inside a run.
- Each scenario has a unique agent identity; A1–A9 card attacks use distinct Host
  aliases (`ATTACK_CARD_HOSTS`), never poison-quiet. Scenario isolation revokes other
  vendor-x agents before each case. Violations filtered by `since`.
- A7 / A8a / A8b run as `-raw` (exam; EXAM_FAILED = caught) and `-patient` (honest for
  EXAM_TASKS, then malicious). Exam must finish EXAM_PASSED or EXAM_FAILED in bounded
  time; stall = infra ERROR.
- A5a expected `{SPOTCHECK, CANARY}`; A8c expected USAGE_MISREPORT only.
- Summary also prints honest-agent task success n/N with vs without adversaries (same
  seeds). Run attacks from the compose network (`docker compose run --rm broker …`).
  Preflight inside that container uses in-network DNS/HTTP probes (no docker CLI).
  `stack up`/`down` and adversary `compose up` use `--remove-orphans` so renamed
  adversary services cannot keep static IPs and cause "Address already in use".
  Preflight requires all profile services healthy, 5/5 honest ACTIVE (waits exams),
  and baseline 3/3 SUCCEEDED before any scenario. Cancel of in-flight tasks is
  best-effort. TracerProvider is process-once; canary interval is an explicit
  `broker_from_settings(..., canary_every=)` argument.
  `stack down` enables every compose profile (derived from the file), `-v`,
  `--remove-orphans`, removes one-off `broker-run-*`, and verifies no bourse
  networks/containers remain. `stack up` refuses leftovers.
  Adversaries start only via `bourse stack up --mode compose --with-adversaries`
  (register + wait-active, then `--profile adversaries`). Do not run raw
  `docker compose --profile adversaries up` in acceptance — leftover postgres from
  db tests plus an unchained adversaries up starts agents while honest exams are
  still PENDING. Wait-active / preflight default timeout is `exam_timeout_s + 120`
  (not a hard 600s); EXAM_FAILED fails immediately and dumps registry exam lines +
  `llm_usage` on `bourse_compose`. Operator sequence: `scripts/acceptance_5b.sh`.
  Meter gate: request > bucket capacity -> 429 `rate_limit_unsatisfiable`; queue
  wait hard-timeout -> 429 `contention`. Capacity must cover max_tokens+max_prompt
  at meter startup. Exams bounded by `exam_timeout_s`; timed-out tasks are
  EXAM_TIMEOUT failures (logged per task).

## Fix round 3 (2026-11-04)

- A5a/A5b **sole-provider** mode: honest `pii.detect` agents marked unavailable via
  `POST /agents/{id}/availability` (selection gate only; TrustState unchanged; restored
  after). Mixed roster (6 runs) is still reported. Sole-provider runs until quarantine or
  12 runs. Expected: A5a caught under sole-provider; A5b may miss with numbers.
- Scenario status **not-exposed** when the adversary never misbehaved (selection gap),
  distinct from **missed**. Table columns: mode, exposure, first_bad, detect_delay, strikes.
- ResidentMonitor: INITIAL_LOAD is per-session baseline and does **not** increment `reloads`
  (the prior "11 INITIAL_LOAD" total was one per broker session, not 11 Ollama loads).

### Honest prefix and exposure (sole-provider, Phase 5b acceptance)

After `/bourse/reset-behavior`, sleepers stay honest for admission exam tasks (`bourse_kind=exam`,
count toward the prefix) plus further e2e tasks (`bourse_kind=task`) until the prefix is spent.
Canaries (`bourse_kind=canary`) are spot-checked and can SOFT-strike, but **do not** consume the
sleeper prefix and are **not** counted in the phase5 `exposure` column (exposure = e2e
delegations to the adversary only).

| Scenario | Behavior after awake | Sole-provider first_bad (1-based exposure) | Notes |
|---|---|---|---|
| A5a | Drop CARD/SIN (spotcheck) | 4 | Mixed mode used 1 honest exposure first; sole then needed 3 more honest e2e before deviate. |
| A5b | Drop PERSON_NAME (grader/canary) | 2 | Mixed mode used 2 honest exposures; sole first_bad 2 = global 4th e2e after exam. |

Observed A5a sole: exposure 5, first_bad 4, detect_delay 1, strikes 3. The third SOFT strike can
come from a canary (spot-checked) while only two *exposure* indices after first_bad are real
e2e — that is expected under the current definition.

**Phase 6 change (not done here):** count canary deliveries in `exposure` (and align
`first_bad` / `detect_delay` to the same index base) so strikes and exposure stay 1:1.

## Status
Accepted (Phase 5b; fix round 3).
