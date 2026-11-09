BOURSE v1.0: an A2A v1.0 multi-vendor agent exchange with deterministic trust, plus BOURSE-Bench
results on a frozen test split. Every number below is generated from the tracked `results/`
directory by `uv run bourse bench report --results` (ADR-0054). Proportions are n/N with Wilson
95% CIs.

Bench provenance: commit `{{commit12}}`, test manifest `{{manifest12}}`, model `{{model}}`
(digest `{{model_digest}}`), {{segments_n}} segments on one {{hw_model}} ({{hw_cpu}},
{{hw_mem_gib}} GiB), host on battery {{host_battery}}, thermal warning {{host_thermal}}.

## Headline

- **Mixed pools (adversaries present):** grader pass B3-mix {{b3_mix.grader_pass}} vs B1-mix
  {{b1_mix.grader_pass}}; attack-caused harm B3-mix {{b3_mix.attack_harm}} vs B1-mix
  {{b1_mix.attack_harm}}. B1-mix's attack-caused harm is mostly billing harm from the
  misreporter ({{b1_mix.billing}}); its attack-caused output harm is {{b1_mix.output_attack}}.
  Raw harmed, which also counts honest-equivalent model errors: B3-mix {{b3_mix.harmed}}, B1-mix
  {{b1_mix.harmed}}.
- **Adversary suite (full stack):** caught {{suite.caught}}, missed {{suite.missed}}; caught before
  harm {{suite.caught_before}}.
- **Marketplace vs single generalist agent (clean pool):** grader pass B3 {{b3.grader_pass}} and B1
  {{b1.grader_pass}} vs B0 {{b0.grader_pass}}.
- **B3 vs B1 on a clean pool: cheaper and faster, not more accurate.** Grader pass B3
  {{b3.grader_pass}} vs B1 {{b1.grader_pass}}. Tokens p50 {{b3.tokens_p50}} vs {{b1.tokens_p50}}.
  Latency p50 {{b3.latency_p50}} s vs {{b1.latency_p50}} s. The {{b1_b3_diff_n}} cases where they
  differ are all {{b1_b3_diff_domains}}.

## What is in the release

- Signed Agent Cards (JCS + JWS, pinned vendor JWKS) and a registry with immutable card revisions,
  card lint, version checks and an admission exam.
- MCP tool servers, a metering proxy for every agent LLM call (per-call deadline, max tokens,
  per-task budgets), five honest agents on local Ollama.
- A LangGraph broker: closed skill taxonomy, selection by reliability lower bound, cost and
  latency, A2A delegation, spot checks, canaries, violation reports.
- Deterministic trust runtime and quarantine; sinkhole and DNS egress capture; Slack approver for
  reinstatement with a fresh reliability epoch.
- BOURSE-Bench: frozen test split with a hash manifest, baselines B0/B1/B3, mixed pools, an
  adversary suite (A1-A9), stack-profile ablations, every metric as n/N.

## Key limits

- The quiet poisoner (card-text attack, honest code) is neutralised by design, but it was never
  quarantined. Reliability-weighted selection sent it {{b3_mix.poison_quiet.served}} of
  {{b3_mix.pii_subtasks}} pii.detect subtasks in B3-mix, because the pii.detect spot check cannot
  see names or false positives.
- The quiet sleeper harms before detection: suite A5b {{suite.a5b_harmed}}; in B3-mix
  {{b3_mix.sleeper_quiet.harmed}} harmed cases, never quarantined. Canaries need at least
  {{canary_min_tasks}} real tasks per agent to strike, so rarely picked agents never get there.
- The spot-check and canary ablations ran on a clean pool, so they measure overhead only.
- Every clean-B3 grader failure is on hard_pii ({{b3.failed_domains}}). pii-precise's filter was
  written knowing the hard_pii generator (validity threat, ADR-0052).
- Token-burner loops survive cancel, and the meter does not refuse revoked agents (future work).
- B2 was dropped, because it cannot be told apart from B1 on a single-model roster. Executor
  speedup is {{speedup_ratio}}x ({{speedup_split}} split).
- Single model, single machine.

Full tables, ablations and every limit:
[RESULTS.md](https://github.com/hamidmatiny/bourse/blob/v1.0.0/RESULTS.md).
