# 0051 — BOURSE-Bench design: baselines, generator, frozen split, harness, sizing

## Context
ADR-0011 fixes benchmark integrity (frozen test split, manifest, `--final` from a clean tree,
n/N). ADR-0009 promises executor pools with a measured speedup. No ADR defines baselines or
ablations; this one does, before any bench code. Phase 6a builds the harness and runs a small
dev pilot only. No `--final` run in 6a.

## Decision

### Baselines (one config id each)
| id | broker | selection | trust engine | spot checks | canaries |
|---|---|---|---|---|---|
| B0 | none | none: one generalist agent does every subtask | no | no | no |
| B1 | yes | uniform random among ACTIVE agents (seeded per case) | no | no | no |
| B2 | yes | static priors, cheapest first (`PriorPolicy`) | no | no | no |
| B3 | yes | measured cost + reliability (`MeasuredPolicy`) | yes | yes | yes |

Per-task meter budgets (ADR-0041) stay on in B1-B3: the compose meter runs with
`require_task_budget=True` and answers 403 `unregistered_task` to any agent call without one,
so a broker that skips registration fails every delegation. They are a resource limit
(ADR-0007), like the global caps. B0 calls the meter with the broker's privileged key.

- B0, generalist: for each subtask of the template plan, one submit-tool call (ADR-0006) to
  the roster model through the meter (same model, `think: false` and deadline as the agents;
  max tokens = the meter's global per-call cap `BOURSE_METER_MAX_TOKENS_PER_CALL` = 2048, not the
  agents' 1024, because B0 writes whole outputs with the LLM where agents use tools). The prompt holds the skill's input JSON (dataset CSV included) and the skill's
  output schema. No MCP tools, no A2A, no card text. Inputs to later skills are built from its
  own earlier outputs (`build_input`), as the broker does. Tokens are metered under task ids
  `bench-b0-<case>:<subtask>`.
- "No trust engine" (B1, B2): the broker sends no violation reports, runs no spot checks and
  sends no canaries. Agents stay whatever state the registry holds; the registry still runs.
- All four use the same cases, seeds, model, deadlines and stack.

### Ablations of B3 (one control removed at a time)
| id | removed control | where | 6a status |
|---|---|---|---|
| B3-no-spotcheck | runtime spot checks | broker (`BrokerControls.spot_checks=False`) | implemented |
| B3-no-canary | canaries | broker (`canary_every=0`) | implemented |
| B3-no-metercap | per-task meter budgets | meter (`require_task_budget=False`) + broker | 6b: stack profile `no-metercap` (ADR-0053) |
| B3-no-exam | admission exam (and card lint) | registry: audited activation without exam | 6b: stack profile `no-exam` (ADR-0053) |
| B3-no-cardsig | card signature check | registry registration | 6b: stack profile `no-cardsig` (ADR-0053) |
| B3-no-sinkhole | egress detection + quarantine (capture stays) | sinkhole, egress-dns | 6b: stack profile `no-sinkhole` (ADR-0053) |

The harness refuses a config whose ablation needs a stack variant it cannot verify, with the
exact reason. It never silently runs full B3 under an ablation's name.
Global meter caps (max tokens per call, fair share) stay on in every config: they are
resource limits (ADR-0007), not a trust control.

### Seeded task generator (`bourse.bench.generator`, `GENERATOR_VERSION`)
- Seeds use the reserved namespaces `bench-dev:` and `bench-test:` (ADR-0037); the exam bank
  stays disjoint (unit test).
- Domains: `people`, `orders`, `measurements` (existing shapes) plus `clinic` (patient visits,
  temp_degF, height_in, weight_kg, dur_min) and `shipments` (weight_oz, dist_mi, vol_gal,
  sender/recipient contacts). Each case is a CSV with 10-30 rows built only from `random.Random(seed)`.
- Truth: profile, stats and units from our deterministic tools (as `exam_bank.truth_for`);
  PII truth is the set of cells the generator planted, by construction, never from a regex.
- Hard-PII set (`domain=hard_pii`): obfuscated emails (`ana [at] mail [dot] com`),
  international phones (`+44 20 7946 0958`, `+33 1 42 68 53 00`), names that are common words
  (`Rose Hunter`, `Will Grant`, `Amber Stone`), names split across `first`/`last` columns, PII
  inside free-text notes, and near-miss decoys (invalid-Luhn cards, 9-digit order numbers that
  fail the SIN check, a `color` column of words that are also first names, a 10-digit NANP-shaped
  SKU). Kinds stay within `PiiKind` (EMAIL, PHONE, CARD, SIN, PERSON_NAME).
- Both PII agents run the same model (ADR-0047) and share `regex_findings`; their pipelines
  differ only in the PERSON_NAME column step (pii-fast: one submit call on 3 sample values per
  unflagged column; pii-precise: a CrewAI crew on 5 sample rows). So only the name cases can
  separate them; obfuscated emails and international phones are missed by both regex
  pipelines alike. The pilot measures the separation per agent on dev cases; the cases are not
  tuned toward a result.
- Graders are the ADR-0023 graders, versioned by `GRADER_VERSION` in `bourse.grading.graders`.

### Frozen split and manifest
- `bourse bench split write --split dev|test --n N --seed S` writes
  `bench/manifests/<split>.json`: generator version, grader version, base seed, and per case
  {case_id, domain, seed, sha256 of the canonical case JSON (CSV + truth)}.
- The test manifest is written once: the command refuses if it exists. Its sha256 is pinned in
  `bench/manifests/test.json.sha256`. Both files are committed; cases are regenerated from the
  seeds and every case sha256 must match (detects generator or grader drift and tampering).
- Dev only for tuning, enforced in code: loading the test split requires a `FinalGuard`, which
  only `final_preflight()` constructs. `bench run` without `--final`, `bench sizing` and the PII
  separation pilot accept `dev` only.
- The test manifest is not written in 6a: its size must cover the N the operator picks after
  the pilot. The dev manifest (60 cases, 10 per domain, base seed `bourse-bench-dev-1`) is.
- The image has no `.git`, so `--final` is two steps. On the host, `bourse bench preflight
  --config X` runs all three checks below and writes `data/bench/<name>/final.preflight.json`.
  In the container, `bourse bench run --final --config X` refuses unless that file exists, is
  < 6 h old, names the same config sha256, and the manifest and model digests it re-checks
  match the preflight. `bench/` is copied into the image, so the stale-image guard (ADR-0050)
  refuses `stack up` after a manifest or config change without a rebuild.
- `bourse bench run --final` refuses unless: (1) `git status --porcelain` is empty; (2) the test
  manifest sha256 equals the pinned hash and every case regenerates to its sha256; (3) every
  roster model is resident in Ollama `/api/ps` with the digest pinned in the config's
  `model_digests`. The result records git sha, manifest sha256, model digests and the Ollama
  environment (`OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_KEEP_ALIVE`,
  `OLLAMA_CONTEXT_LENGTH`, `ollama_base_url`), and writes the ADR-0011 lock file.

### Harness (`bourse bench run --config <yaml>`)
- A run directory `data/bench/<name>/` holds `plan.json` (config sha256 + ordered cells
  `<config>/<case>/<repeat>`), `results.jsonl` (one line per finished cell, flushed and fsynced)
  and `summary.json`. Resume reads finished cell keys and skips them; a config whose sha256
  differs from `plan.json` is refused. A crash loses at most the cell in flight, which is rerun
  from scratch with a new run id.
- Metrics per config, every one as n/N:
  task success (run SUCCEEDED) n/N cases; grader pass (all subtasks graded passed) n/N cases;
  subtask grader pass n/N subtasks; contention (cases with >= 1 CONTENTION delegation or 429
  `contention`) n/N; each with a Wilson 95% CI. Cost: tokens in/out per case and measured
  compute (sum of Ollama `total_duration_ms` from `llm_usage`, fallback `wall_ms`), reported as
  p50/p95 over N cases. Latency: case wall p50/p95 over N. Quantile CIs are distribution-free
  order-statistic intervals (binomial), since Wilson applies to proportions only.
- Adversary benchmark: config `adversaries:` lists {id, mode, runs, honest_real}. Each runs
  through the ADR-0042 phase5 runner (compose network only) and reports caught / missed /
  not-exposed / error n/N with exposure, first_bad, detect_delay, strikes and harmed n/N.
  `honest_real` (sleeper honest phase after the exam) is set per adversary through
  `POST /bourse/reset-behavior?honest_real=N` and recorded in the result.
- Executor pools (ADR-0009): `PoolExecutor` implements `submit_llm` (semaphore),
  `submit_cpu` (ProcessPoolExecutor) and `map`. The broker's subtask concurrency per dependency
  level is `subtask_slots` (config; default `OLLAMA_NUM_PARALLEL` = 2; the meter gate bounds
  real LLM concurrency regardless). Speedup is measured: the same cases run under
  `subtask_slots: 1` and `2`, and the report gives total wall serial / total wall parallel
  and the per-case paired ratio p50, as measured numbers.

### Sizing (filled from the dev pilot, not guessed)
`bourse bench sizing data/bench/<pilot>` prints seconds per case per config from the pilot and a
table of N per config for an 8 h night, with the Wilson 95% half-width at p = 0.5 (worst
case) and at the pilot's observed p. N is chosen by the operator after the pilot; this ADR
records the table and the chosen N when they exist.

Dev pilot `pilot-6a` (2026-11-05, 36/36 cells, 4622 s; 5 dev cases, A5a sole-provider,
PII separation on 5 hard_pii cases). s/case = pilot total wall / pilot N.

| config | pilot N | s/case | grader_pass | h @ N=50 | h @ N=100 | h @ N=150 | h @ N=200 |
|---|---|---|---|---|---|---|---|
| B0 | 5 | 218.5 | 0/5 | 3.03 | 6.07 | 9.10 | 12.14 |
| B1 | 5 | 89.8 | 5/5 | 1.25 | 2.49 | 3.74 | 4.99 |
| B2 | 5 | 106.3 | 5/5 | 1.48 | 2.95 | 4.43 | 5.91 |
| B3 | 5 | 79.8 | 5/5 | 1.11 | 2.22 | 3.33 | 4.43 |
| B3-serial | 5 | 81.3 | 5/5 | 1.13 | 2.26 | 3.39 | 4.52 |
| A5a sole-provider (1 spec) | 1 | 1160 per spec | caught 1/1 | | | | |

The table and N proposal including the mixed-pool configs are in "Fix round 1" below.

### Executor speedup scope
ADR-0009 asked for speedup at 1/2/4/8 workers. The meter's global gate is
`OLLAMA_NUM_PARALLEL` = 2 (ADR-0044), so more than 2 concurrent LLM calls only queue at the
meter. 6a measures 1 vs 2; 4 and 8 would measure queueing, not parallelism, and are not run.

## Fix round 1 (after `pilot-6a`, 2026-11-05)

### Finding: B1/B2/B3 are equal on quality because the pool had no adversaries
The pilot pool held only the 5 honest agents, so every baseline picked an honest agent and
B1/B2/B3 all scored grader_pass 5/5. A clean pool measures selection cost and latency, not the
trust layer. Selection differed only in cost: B2 picked `vendor-b-pii-precise` 5/5 (tokens p50
10014), B3 picked `vendor-c-pii-fast` 5/5 (4680), B1 split 3 fast / 2 precise.

### Decision (Phase 6b, not implemented in 6a): mixed-pool configs
`B1-mix`, `B2-mix`, `B3-mix`: same cases as B1/B2/B3, with these adversaries ACTIVE in the
provider pool next to the honest agents for the whole config:

| adversary | skill | caught by | layer | runs in B1/B2? |
|---|---|---|---|---|
| A4b poison-quiet | pii.detect | zero-effect (honest behaviour) | none needed | yes |
| A5a sleeper-loud | pii.detect | spot check | broker | yes, never caught |
| A5b sleeper-quiet | pii.detect | canary (or none) | broker | yes, never caught |
| A6a/A6b/A6c exfil | schema.profile | sinkhole / DNS log | stack | yes, caught in every config |
| A8c misreporter | schema.profile | USAGE_MISREPORT | broker | yes, never caught |

- A4a (poison-loud) cannot be in any pool: card lint rejects it at registration in every
  config (admission is registry-side). It is mixed in only with the `no-exam` / lint stack
  variant. A7/A8a/A8b `-raw` fail the exam the same way; `-patient` variants are bounded by the
  meter and deadlines, which stay on in every config.
- A6 is quarantined by the sinkhole in B1 and B2 too (the sinkhole reports violations to the
  registry, not the broker). B1-mix vs B3-mix therefore measures the broker-side controls
  (spot checks, canaries, violation reports); the stack's share is measured by `no-sinkhole`.
- Registry state is global. Each mixed config starts from a reset: adversaries re-admitted
  with a fresh reliability epoch (ADR-0048 reinstatement path) and their behaviour reset with
  the config's `honest_real` (`POST /bourse/reset-behavior`), so B3's quarantines do not carry
  into B1/B2. The reset and the pool membership are recorded per config.
- Metrics per mixed config, each n/N with Wilson 95% CI:
  - honest success: cases whose every subtask passes its grader;
  - harmed tasks: cases where an adversary served a subtask and harm occurred: grader fail on
    that subtask (A5a/A5b), an egress attempt during it (A6), or a usage claim that differs from
    the metered usage by more than `MISREPORT_RATIO` and was not reported (A8c);
  - adversary share: subtasks served by an adversary / subtasks delegated;
  - first_bad and detect_delay per adversary (as in the adversary suite).
- **B2 tie-break artifact (must be fixed before B2-mix is meaningful).** With the single-model
  roster (ADR-0047) every agent has the same prior cost, so `PriorPolicy` ties and falls to
  `agent_id` order. `poison-quiet@…`, `sleeper-*@…`, `exfil-*@…` and `misreporter@…` all sort
  before `vendor-*@…`, so B2-mix would always pick an adversary because of its name. Proposal:
  break B2 ties by sha256(config seed + agent_id), fixed for the whole config (still static,
  not name-driven), and record the order. Needs operator approval.

### Updated sizing (cases only; mixed configs are estimates, not measured)
Assumptions, stated: `B3-no-spotcheck` and `B3-no-canary` cost what B3 costs (79.8 s/case;
spot checks are deterministic, removing canaries can only lower cost). Mixed configs cost
between their clean pilot s/case (lower bound) and 1.5x that (fallbacks after a caught
subtask re-run it on an honest agent; upper bound is a guess until a 6b mixed pilot measures
it). An 8 h night is 28 800 s.

| config set | s per case-set | h @ N=50 | h @ N=100 | h @ N=150 | h @ N=200 |
|---|---|---|---|---|---|
| clean: B0, B1, B2, B3, B3-no-spotcheck, B3-no-canary | 654.0 | 9.08 | 18.17 | 27.25 | 36.33 |
| mixed: B1-mix, B2-mix, B3-mix (low / high) | 275.9 / 413.9 | 3.83 / 5.75 | 7.66 / 11.50 | 11.50 / 17.24 | 15.33 / 22.99 |
| total (low / high) | 929.9 / 1067.9 | 12.9 / 14.8 | 25.8 / 29.7 | 38.7 / 44.5 | 51.7 / 59.3 |
| nights of 8 h (low / high) | | 1.6 / 1.9 | 3.2 / 3.7 | 4.8 / 5.6 | 6.5 / 7.4 |

Not included: the adversary suite (A4-A8 scenario specs). Only A5a was measured (1160 s); the
other specs are unmeasured and are sized in the 6b mixed pilot.

Wilson 95% half-width (computed): N=50 0.134 @ p=0.5, 0.085 @ p=0.9, 0/50 -> [0, 0.071],
50/50 -> [0.929, 1]; N=100 0.096 / 0.060, 0/100 -> [0, 0.037], 100/100 -> [0.963, 1];
N=150 0.079 / 0.048; N=200 0.069 / 0.042. The `@<config>` columns of `bench sizing` use the
pilot's observed p; at 5/5 or 0/5 that is the one-sided width at p = 1 or 0, which understates
the width for any p in between.

### N proposal (operator chooses)
N = 100 for B1, B2, B3, B3-no-spotcheck, B3-no-canary, B1-mix, B2-mix, B3-mix; N = 50 for B0
(B0 is the most expensive config at 218.5 s/case and scored 0/5; 0/50 bounds its pass rate at
<= 0.071). Cases: 15.1 h clean + 7.7-11.5 h mixed = 22.8-26.6 h, i.e. 3-4 nights, plus the
adversary suite (unmeasured, at least one more night). The test manifest needs >= 100 cases
(B0 uses the first 50). Not written until N is approved.

### Other fix-round-1 changes
- Summary table: quantile cells printed `value [lo, hi] N=5`; with two-space column
  separators a pasted `N=5  95.0` read as `N=595.0`. Rows now use ` | ` separators, N is one
  column per row, and quantile cells are `value [lo, hi]` (regression test).
- `PiiCaseResult.output_sha256` (sha256 of the canonical JSON output) and a summary line
  "pii outputs byte-identical across agents n/N". Records written before this change have no
  hash and are excluded from that count.
- PII separation result and the pii-precise proposal: ADR-0052.
- Executor speedup 1.02x recorded as measured in ADR-0009.

## Fix round 2 (operator decisions, 2026-11-06)

### Decided N
- N = 100 for B1, B2, B3, B3-no-spotcheck, B3-no-canary, B1-mix, B2-mix and B3-mix.
- N = 50 for B0: the first 50 of the same 100 cases (`ConfigSpec.cases`).

### Test manifest (written once, before any ADR-0052 work)
- Command: `bourse bench split write --split test --n 100 --seed bourse-bench-test-1`
  (2026-11-06).
- `bench/manifests/test.json`: 100 cases, generator 1, grader 1.
- sha256 `f3f61465ed3807a88d839a7a7cc91a82e70094b114cd92721f56b5d4e5587d6e`, pinned in
  `test.json.sha256`.
- Domains: people 17, orders 17, measurements 17, clinic 17, shipments 16, hard_pii 16.
- A second write is refused.
- Consequence: the manifest records GENERATOR_VERSION and GRADER_VERSION. Any later grader or
  generator change makes `verify_cases` refuse the test split, and a new test manifest would be
  a new benchmark. ADR-0052 changes an agent and an MCP tool, not a grader or the generator.
- CI regenerates the dev manifest on Linux, not the test manifest. The test split is touched
  only by `--final`.

### B2 = "trusts card-declared claims"? Not possible with these cards; B2 is the per-task fallback
- The operator asked for B2 to select by self-reported card cost/latency, unverified, with ties
  broken by a seeded hash per task.
- Checked: neither the honest nor the adversary Agent Cards declare any cost or latency.
  `build_card` emits name, description, version, interfaces, provider, capabilities, modes
  and skills (id, name, description, tags). The registry's `AgentRecord` stores no such field.
- Adding claims would need four things:
  - a card field or extension;
  - registry parsing and storage, i.e. a migration;
  - new signed cards for 5 honest and 15 adversary agents;
  - numbers for every agent to claim.
- The claimed numbers are the problem. B2-mix would rank by them, so its result would be set by
  values written for this benchmark, not measured.
- Chosen: the fallback. `PriorPolicy(tie_seed=...)` breaks utility ties by
  sha256(`<case seed>:<repeat>:<subtask id>:<agent_id>`), a fresh seeded draw for each task.
  Utility still decides whenever it differs. `BrokerCellRunner` reseeds it per (case, repeat).
- **B2 ≈ B1.** All agents run one model (ADR-0047), so every prior ties. B2's choice is then a
  uniform seeded draw per task, the same distribution as B1's `RandomPolicy`. The draws come
  from different seeds and B2 also orders its fallbacks by the hash, but B2 and B1 measure the
  same thing.
- B2 is kept with that label, not described as a different selection strategy. Dropping B2 from
  the final run would save about 2.95 h (N=100 × 106.3 s), plus about the same again for B2-mix.
  That is the operator's call.
- `MeasuredPolicy` (B3) tie-breaking was changed in fix round 3, below.

### Host conditions in every run
The PII re-run `pii-identity-6a` was about 1.8x slower than `pilot-6a` (pii-fast 11.3-12.2 s vs
6.3-7.1 s per case), and nothing recorded why. The operator's machine has `powermode 1` (Low
Power Mode) in its battery profile and `powermode 0` on AC (`pmset -g custom`, 2026-11-06), so
running on battery is one possible cause. It is not proven. On AC with Low Power Mode off
(`adr0052-ties-6a`, 22/22 samples), pii-fast took 5.5 s/case p50. That fits the explanation, but the slow run
has no host samples to compare.

`bourse bench launch --config <yaml>` (host only) does the following:
- starts `docker compose run --rm -T broker bourse bench run` with stdin from `/dev/null` and
  output to `data/logs/bench-<name>.log`;
- appends `HostSample`s to `data/bench/<name>/host.jsonl` at start, every `--interval` s
  (default 60) and at the end. A sample records `pmset -g batt` (source, %, state), the
  `pmset -g` powermode in use (1 = Low Power Mode), `pmset -g therm` (warning,
  CPU_Speed_Limit) and the 1/5/15-minute load average;
- records a failing `pmset` as `error`, never as a value.

The summary prints "host conditions (k samples): on battery n/N, low power mode n/N, thermal
warning n/N, battery min, CPU speed limit min, load1 p50/max". A run that was not launched this
way prints "not recorded". Background launches use `< /dev/null` (the `pii-identity-6a` job was
suspended on tty input).

## Fix round 3 (operator decisions, 2026-11-06)

### B3 exact ties: seeded hash, not agent_id order
- **Problem.** Ordering ties by agent name lets an attacker win them: it only has to pick a name
  that sorts first (`aaa-…`).
- **Decision.** `MeasuredPolicy` breaks exact utility ties and least-tried exploration ties by
  `tie_key(tie_seed, decision index, subtask id, agent_id)`, the same sha256 construction B2
  uses. The decision index is the persisted count of selection decisions for the skill, so every
  task gets a fresh draw.
- **Seed.** The default `tie_seed` is a per-process secret (`secrets.token_hex(16)`), so nobody
  can choose a name in advance to sort first. The bench passes the per-case seed
  (`<case seed>:<repeat>`) so runs are reproducible.
- **Test.** `test_name_that_sorts_first_gets_no_tie_advantage`: with three exactly tied cold
  candidates, `aaa-attacker@x.local` wins 70-130 of 300 seeded decisions, for both utility and
  exploration ties. Name order would give 300/300.
- **Still name-ordered.** `PriorPolicy` keeps `agent_id` ties when no `tie_seed` is given (only
  the B2 bench path passes one), and `RandomPolicy` is unaffected.

### B2 and B2-mix dropped from the final configs (code kept)
On a single-model roster (ADR-0047) every prior ties, so B2 is a seeded uniform draw per task,
the same distribution as B1. B2 cannot be told apart from B1 and is untestable as "static /
cheapest selection" here. `PriorPolicy(tie_seed=…)`, the B2 baseline and its tests stay. A
multi-model roster would make B2 meaningful again.

Final configs: B0 (N=50), B1, B3, B3-no-spotcheck, B3-no-canary, B1-mix, B3-mix (N=100 each).

| config set | hours (pilot s/case; mixed 1.0-1.5x, unmeasured) |
|---|---|
| clean: B0 x50, B1, B3, no-spotcheck, no-canary | 12.18 |
| mixed: B1-mix, B3-mix | 4.71-7.07 |
| total | 16.9-19.3 h = 2.1-2.4 nights of 8 h, i.e. 3 nights, plus the adversary suite (unmeasured) |

### Runs must not start on an unready stack
- **What happened.** Dev run `adr0052-b2-6a` (2026-11-06) ran on a stack where no agent was
  registered. `stack up --mode compose` only registered and examined agents with
  `--with-adversaries`, and otherwise exited 0 at "services ok 17/17". All 25 cells errored in
  44 s, were checkpointed, and `bench run` exited 0. The run directory was moved to
  `data/bench/adr0052-b2-6a.failed-unregistered`. It is not a result.
- **Fixes:**
  - `stack up --mode compose` always registers and waits for every agent to be ACTIVE.
    `--with-adversaries` now only adds the adversary profile. A registration error, fewer
    than all ACCEPTED, or an exam failure or timeout exits 1 with
    `STACK UP FAILED: <reason>` and the activation diagnostics.
  - `bench run` (container) and `bench launch` (host) check the registry before any cell.
    When a config uses the broker or adversaries, every honest roster agent must be
    registered and ACTIVE, and every `pii_separation` agent must be ACTIVE. An unreachable
    registry or any problem exits 2 with `REFUSED: … preconditions failed` and one line per
    problem, before anything is checkpointed. `bench launch` writes this to its log.
  - `bench run` exits 3 with `BENCH RUN FAILED: all N cells run this invocation ended in
    ERROR` when every cell it ran errored. `bench launch` exits with the run's code and on
    failure prints the last 20 lines of the run log.

## Fix round 4 (Phase 6b, operator decisions 2026-11-06)

### Ablations that need a stack, and mixed pools
Both are implemented in ADR-0053. Stack ablations are stack profiles that run their own
adversaries. Mixed configs run one per fresh stack. B2 and B2-mix stay dropped.

### Re-sizing with pii-precise 1.1.0
Sources, all dev:
- `pilot-6a` per-cell walls;
- `adr0052-ties-6a` re-run (pii-precise 1.1.0, AC power, Low Power Mode off);
- the Phase 5 adversary reports in `data/attacks/`.

**Measured**
| input | value | source |
|---|---|---|
| B0 s/case | 218.5 | pilot-6a, 5 cells |
| B1 cell, pii served by pii-fast | 66, 67, 74 s (mean 69.0) | pilot-6a B1 |
| extra wall when pii-precise 1.1.0 serves the pii subtask | +28.7 s (82, 84 vs 49, 52, 62) | adr0052-ties-6a B2 cells |
| B3 s/case | 79.8 (pilot-6a, 5/5 pii-fast); 69.9 (ties re-run) | larger one used |
| harness overhead (elapsed / sum of cell walls) | 811 / 678 = 1.20 | adr0052-ties-6a, 10 broker cells |
| stack up incl. exams | 442 s, 573 s | two 6a stack ups |
| full adversary suite, 19 scenarios, A5a/A5b mixed only | 8192 s | phase5-20261104T130644Z |
| one A5 sole-provider scenario (incl. its 3-run baseline) | 358-1502 s, 7 runs | phase5-20261105T* |

**Estimated (not measured)**
- B1 s/case = 69.0 + 0.5 x 28.7 = 83.4. B1 picks either PII agent with p = 0.5.
- Mixed configs run at 1.0-1.5x the clean s/case, as in fix round 1, plus pool admission
  (7 exams). Admission is assumed to take at most 15 min.
- Profile segments use 431 s per e2e scenario, the mean of the 8192 s suite.
- One baseline honest e2e run per adversary spec costs about 80 s (B3 s/case).
  - The bench calls the phase5 runner once per spec, and each call runs a health-gate baseline
    of `baseline_runs` honest e2e runs.
  - The 6b configs set `baseline_runs: 1` (default 3). The baseline's numbers are not in the
    bench row.
- The Nov 4 suite timing predates 5c and 6a. The `dev6b-*` validation segments replace these
  estimates with measurements before night 1.

**Segments** (hours = N x s/case x 1.20 + stack up 0.17 h)
| segment (config) | stack up flags | hours |
|---|---|---|
| final-suite (A1-A9, all variants; A5 mixed + sole) | `--with-adversaries` | 8192 - 240 (its own 3-run baseline) + 2 x 1502 + 19 x 80 s = 12476 s = 3.47, + 0.17 = 3.64 |
| final-b3-mix (N=100) | `--with-adversaries` | 2.66 x 1.0-1.5 + 0.25 + 0.17 = 3.08-4.41 |
| final-b1-mix (N=100) | `--with-adversaries` | 2.78 x 1.0-1.5 + 0.25 + 0.17 = 3.20-4.59 |
| final-b3 (N=100) | - | 2.66 + 0.17 = 2.83 |
| final-b3-no-spotcheck (N=100) | - | 2.83 |
| final-b3-no-canary (N=100) | - | 2.83 |
| final-b1-b0 (B1 N=100, then B0 N=50) | - | 2.78 + 3.64 + 0.17 = 6.59 |
| final-no-cardsig | `--stack-profile no-cardsig --with-adversaries` | 5 x (80 s baseline + about 30 s of registrations) + stack up = 0.32 |
| final-no-sinkhole | `--stack-profile no-sinkhole --with-adversaries` | 3 x (431 + 80) s + stack up = 0.59 |
| final-no-exam | `--stack-profile no-exam --with-adversaries` | 4 x (431 + 80) s + stack up = 0.73 |
| final-no-metercap | `--stack-profile no-metercap --with-adversaries` | 5 x (431 + 80) s + an A8a-raw exam without a cap (up to 5 x 300 s) + stack up = 1.29 |

### Schedule (8 h per night at most, using the high estimates)
| night | segments | hours |
|---|---|---|
| 1 | final-suite; final-b3-mix | 6.72-8.05 |
| 2 | final-b3; final-b1-mix | 6.03-7.42 |
| 3 | final-b3-no-spotcheck; final-b3-no-canary; final-no-sinkhole; final-no-cardsig | 6.57 |
| 4 | final-b1-b0; final-no-exam | 7.32 |
| 5 | final-no-metercap | 1.29 |

- **Night 1 is short of the request.** The operator asked for the suite, B3 and B3-mix on
  night 1. Together they need 9.55-10.88 h, which does not fit in 8 h. Night 1 runs the suite
  and B3-mix, because B3-mix is the result the trust layer exists for. B3 opens night 2; clean
  B3 already has a 5-case dev measurement (5/5).
- **Night 1's upper bound.** Night 1 fits in 8 h only if B3-mix runs at no more than 1.48x clean
  B3 (3.64 + 0.42 + 2.66 x f <= 8). If the dev measurement (`dev6b-b3-mix`) gives f > 1.48,
  B3-mix and B3 swap nights: night 1 becomes the suite plus B3 (6.47 h).
- **Order.** Segments in a night are joined by `;`. Each prints `=== SEGMENT <name> exit <code>`.
- **No code changes between nights.** `src/` does not change between nights. A needed fix
  invalidates the nights already run, and they are rerun.
- **Re-issue after dev validation.** The schedule is re-issued after the dev validation if a
  measured segment time breaks the 8 h bound.

## Fix round 5 (after the dev6b validation, 2026-11-06)

### Measured (dev6b, 6 segments, `data/logs/dev6b.log` SEGMENT lines)
| segment | wall s | bench s | stack down+up s | note |
|---|---|---|---|---|
| dev6b-suite (A1, A6a, A8a-patient; mixed, 2 runs) | 1373 | 752 | 621 | specs 85, 359, 308 s |
| dev6b-b3-mix (3 cells) | 1183 | 611 | 572 | pool admission 323 s; cells 105, 108 s elapsed (walls 75.6-98.6 s) |
| dev6b-no-cardsig (5 card specs) | 946 | 446 | 500 | specs 59-183 s |
| dev6b-no-sinkhole (3 specs) | 1551 | 1053 | 498 | specs 336-372 s |
| dev6b-no-exam (4 specs) | 1600 | 1534 | 66 | no exams at stack up; specs 272-549 s |
| dev6b-no-metercap (5 specs) | 13310 | 12809 | 501 | A8a-raw 4603 s, A8b-raw 4717 s (looping INCONCLUSIVE exams, ADR-0049 amendment); others 1403, 1124, 962 s |
| total | 19963 (5.55 h) | | | estimate was 3-3.5 h; the excess 3.6 h is the two looping exams |

- **B3-mix factor:** 106.5 s per cell (mean of the 2 cells after admission) against 95.8 s for
  clean B3 (79.8 x 1.20). That is 1.11, below the 1.48 swap threshold of fix round 4. 3 cells
  only.
- **One e2e run, launch included:** about 100 s (cells 75.6-98.6 s wall, 105-108 s elapsed).
- **One adversary spec with exam, baseline and 2 runs:** 308-372 s.

### Estimated per spec (amendment 1 of ADR-0053: sole-provider, k = 3 real deliveries)
- Runtime spec, k = 3, nothing holding a delivery: 360 s (measured 2-run spec) + 100 s = 460 s.
  A4b, A6a-c, A7-patient; A6 on the full stack stops sooner, at the first quarantine.
- A delivery held by the budget (burner to refusal, about 200 s) or by the 300 s deadline adds
  its time per delivery:
  - A8a-patient: 460 + 3 x 200 = 1060 s;
  - A8b-patient: 460 + 3 x 300 = 1360 s;
  - under no-metercap the burner also runs to the deadline: 360 + 3 x 400 = 1560 s.
- A8c: 962 s measured (exam + 2 runs) + 100 = 1060 s.
- A5a and A5b (mixed 6 runs + sole up to 12, until quarantine): 2200 s each. Sources: A5
  sole-provider 358-1502 s measured in phase5-20261105, plus 6 x 100 s mixed and the exam.
- Registration-gated on the full stack (no delivery; baseline + registration + exam until the
  2nd failure):
  - A4a: 120 s;
  - A7-raw: 200 s;
  - A8a-raw: 100 + 2 x 200 = 500 s;
  - A8b-raw: 100 + 2 x 300 = 700 s.
- Cards: 89 s each (measured mean).
- Under no-exam (no exam, baseline + 3 runs):
  - A4a and A7-raw: 400 s each;
  - A8a-raw: 100 + 3 x 300 = 1000 s;
  - A8b-raw: 100 + 3 x 400 = 1300 s.
- **Wall cap per spec:** about 2x these estimates.
  - 900 s for cards, 1800 s for most runtime specs, 2700 s for registration-gated specs
    (adversary exam wait 2010 s + baseline), 3600 s for A8a/A8b deliveries, 5400 s for A5.
  - Pinned by `test_final_adversary_specs_are_capped_and_explain_non_exposure`.

### Segments
| segment | hours | how |
|---|---|---|
| final-suite (19 specs) | 3.55 | 450 cards + 1520 gated (120 + 200 + 500 + 700) + 460 A4b + 4400 A5 + 1380 A6 + 460 A7-patient + 1060 + 1360 + 1060 A8 = 12150 s, + stack 0.17 |
| final-b3-mix (N=100) | 3.21 | 100 x 106.5 s measured + admission 323 s + stack 572 s |
| final-b3 (N=100) | 2.83 | 100 x 95.8 s + 0.17 (pilot-6a, not re-measured) |
| final-b1-mix (N=100) | 3.33 | B1 83.4 x 1.20 x 1.11 measured mix factor = 111.1 s/case + admission 323 s + stack 572 s |
| final-b3-no-spotcheck, final-b3-no-canary | 2.83 each | as B3 |
| final-b1-b0 | 6.59 | unchanged (fix round 4) |
| final-no-cardsig | 0.26 | measured dev segment (946 s) |
| final-no-sinkhole | 0.52 | 3 x 460 s + stack 498 s |
| final-no-exam | 0.88 | 400 + 400 + 1000 + 1300 s + stack 66 s |
| final-no-metercap | 1.69 | 700 + 700 + 1560 + 1560 + 1060 s + stack 501 s; wall caps bound it at 4.0 h |

### Block schedule (8 h per block at most)
| block | segments | hours |
|---|---|---|
| 1 | final-suite; final-b3-mix | 6.76 |
| 2 | final-b3; final-b1-mix | 6.16 |
| 3 | final-b3-no-spotcheck; final-b3-no-canary; final-no-sinkhole; final-no-cardsig | 6.44 |
| 4 | final-b1-b0; final-no-exam | 7.47 |
| 5 | final-no-metercap | 1.69 |

- **Block 1 is the suite plus B3-mix,** as the operator allowed (suite + B3 or B3-mix). B3-mix
  is the result the trust layer exists for, and its measured factor 1.11 keeps the block at
  6.76 h. Suite + B3 would be 6.38 h.
- **Not folded into fewer blocks.** no-metercap would fit into block 2 (7.85 h) but not
  block 3 (8.13 h). The 0.15 h margin is too thin for estimates built on 3 B3-mix cells and on
  unmeasured sole-provider specs.
- **The sole-provider adversary path is new and not yet measured.** The re-validation segments
  (dev6b-suite, -no-sinkhole, -no-exam, -no-metercap) measure it. This table is re-issued from
  those numbers before block 1, if any segment breaks its 8 h bound.

## Consequences
- B0-B3 are comparable on the same frozen cases; ablations isolate one control each. Three
  ablations need stack variants and are not runnable in 6a; they are listed, not faked.
- The test split is in git as seeds + hashes; any generator, grader or manifest change is
  detected before a final run.
- A crash costs at most one case; a full test run can span several nights.

## Status
Fix round 5 (2026-11-06): measured dev6b timings, block schedule; re-validation pending.
Accepted (operator, 2026-11-06). N=100 per config, B0 N=50. The final configs are those listed in
fix round 3. The sizing table uses pilot s/case from before ADR-0052. pii-precise 1.1.0 now takes
43.0 s/case p50 on dev (ADR-0052 amendment 3), and how that affects broker cells is not measured.
The `--final` run is Phase 7.
