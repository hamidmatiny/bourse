# 0054 — Reproducible results: tracked results/ dir and a generated RESULTS.md

## Context
Phase 7 reports the final BOURSE-Bench run. The bench writes everything under `data/bench/`,
which is never committed (AGENTS.md rule 13). The trust evidence behind several findings
(ledger rows, spot-check and canary outcomes, selection decisions, violations) lives in the
stack database, which each segment's `stack down` removes. A RESULTS.md with hand-typed numbers
could drift from the data and could not be checked by anyone without `data/`.

Two facts about the final data matter here:
- final-suite's database was removed by the next segment's stack down before the evidence
  export (`data/scripts/export_evidence.sh`) existed. It has no evidence export.
- Block 3 run 1 was aborted by an operator double launch and is archived under
  `data/bench/aborted/`. Block 3 was re-run clean on the same commit; only the re-run is used.

## Decision
- **`results/` is tracked.** `uv run bourse bench results-export` copies, for each of the 11
  final segments, `summary.json`, `results.jsonl` and `provenance.json` unchanged into
  `results/<segment>/`. It refuses unless every segment has `final=True` and `git_dirty=False`,
  and all share one commit, one test manifest and one model digest.
- **Evidence is reduced to counts.** `results/<segment>/evidence.json` (contract
  `EvidenceDigest`, `src/bourse/contracts/results.py`) holds:
  - violations per (agent, kind);
  - ledger rows per (agent, skill, kind, state, spot_check_passed);
  - selection decisions per (skill, chosen, reason);
  - the candidate terms of the last pii.detect decision.

  Violation evidence text, honeytokens, agent outputs and database ids are not copied. A
  segment without an export gets `available=False` (final-suite).
- **Supporting evidence**, labelled as such in RESULTS.md:
  - `results/supporting/pilot-6a/summary.json`: the executor speedup, dev split.
  - `results/supporting/phase5c.json` (contract `Phase5cEvidence`), parsed from the Phase 5c
    acceptance logs. Slack approver user ids and request ids are dropped.
  - `results/host.json` (contract `HostHardware`): machine model, CPU, memory, OS, Ollama and
    Docker versions, read at export time on the machine that ran the blocks, because
    `host.jsonl` records power and load, not hardware.
- **RESULTS.md is generated.** `uv run bourse bench report --results` renders
  `docs/results/RESULTS.template.md`:
  - The template holds the prose and `{{key}}` placeholders. Every value comes from `results/`
    (`src/bourse/bench/results_report.py`), or from a code constant (canary interval, sleeper
    trigger).
  - An unknown placeholder is an error.
  - The same command fills the block between `RESULTS:BEGIN` and `RESULTS:END` in README.md
    from `docs/results/README.results.template.md`.
  - The few numbers quoted from earlier measurements that are not in `results/` (ADR-0048 0/5,
    ADR-0052 contact_check overlap, ADR-0053 token-burner calls) stay in the template text and
    each names its ADR.
- **CI checks drift.** `uv run bourse bench report --results --check` runs in the `check` job
  and fails if RESULTS.md or the README block differs from what `results/` and the templates
  produce.
- **Harm by type** is a reporting split of the existing per-cell fields; nothing is
  reclassified.
  - output: `harm_detail` part "grader fail on …";
  - billing: "usage misreport";
  - egress: an exfil agent in `attempt_agents`, and how many of those cells were harm.

## Consequences
- Anyone with the repo can regenerate RESULTS.md and the README numbers without `data/` or a
  running stack, and CI keeps them in sync.
- results/ adds about 1.3 MB of JSON to the repo.
- The evidence-derived tables (spot checks, canaries, selections) exist for every final segment
  except final-suite.
- No behaviour changes: the bench, broker, registry and meter are untouched. The new code reads
  files and writes `results/`, RESULTS.md and the README block.

## Status
Accepted (Phase 7).
