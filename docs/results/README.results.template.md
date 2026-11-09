Final BOURSE-Bench run on the frozen test split (commit `{{commit12}}`, manifest `{{manifest12}}`,
one model `{{model}}` on one machine). n/N with Wilson 95% CIs; full tables, ablations and
limits in [RESULTS.md](RESULTS.md).

| config | task success | grader pass | harmed cases | tokens p50 | latency s p50 |
|---|---|---|---|---|---|
| B1 clean (random) | {{b1.task_success}} | {{b1.grader_pass}} | - | {{b1.tokens_p50}} | {{b1.latency_p50}} |
| B3 clean (measured) | {{b3.task_success}} | {{b3.grader_pass}} | - | {{b3.tokens_p50}} | {{b3.latency_p50}} |
| B1-mix (adversaries, broker trust controls off) | {{b1_mix.task_success}} | {{b1_mix.grader_pass}} | {{b1_mix.harmed}} | {{b1_mix.tokens_p50}} | {{b1_mix.latency_p50}} |
| B3-mix (adversaries, full trust layer) | {{b3_mix.task_success}} | {{b3_mix.grader_pass}} | {{b3_mix.harmed}} | {{b3_mix.tokens_p50}} | {{b3_mix.latency_p50}} |

- Adversary suite: caught {{suite.caught}}, missed {{suite.missed}}; caught before harm
  {{suite.caught_before}}.
- B3 on a clean pool is cheaper and faster than B1, not more accurate; every clean-B3 grader
  failure is on hard_pii ({{b3.failed_domains}}).
- The quiet poisoner (honest code, card-text attack) was never caught, and reliability-weighted
  selection sent it {{b3_mix.poison_quiet.served}} of {{b3_mix.pii_subtasks}} pii.detect subtasks in B3-mix
  (RESULTS.md, finding 1).
