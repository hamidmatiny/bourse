# 0037 — Admission exam, canaries, exam/bench disjointness

## Context
Phase 3 activated agents by hand (ADR-0028). Activation should follow measured behaviour, and
behaviour should keep being checked after listing without the agent knowing when.

## Decision
- Exam bank (`trust/exam_bank.py`): seeded generator of 8-12 row datasets (names, emails,
  phones, Luhn-valid and invalid cards, valid and invalid SINs, ages with nulls, weight_lb,
  dist_km, dates); truth computed with our deterministic tools. Seed namespaces: "exam:" for
  exams and canaries, "bench-dev:" and "bench-test:" reserved for BOURSE-Bench. A unit test
  generates 200 datasets per namespace and checks the sha256 sets are pairwise disjoint.
- Honeytokens: HMAC-SHA256(secret, task_id) rendered as a Luhn-valid 16-digit card and an email,
  written into row 0 of exam and canary datasets (truth includes them), stored in the
  append-only `honeytokens` table and matched by the sinkhole (ADR-0035).
- Exam: an accepted registration that leaves the agent PENDING schedules a background exam in
  the registry: k = 5 tasks per declared skill, graded with the Phase 2b graders against the
  generated truth. >= 4/5 for every declared skill -> ACTIVE (actor "exam", reason "admission
  exam passed n/N"). Otherwise the agent stays PENDING with a SOFT `EXAM_FAILED` violation; no
  new exam starts within the cooldown (default 600 s). Exam messages use the delegation format
  and task ids `run-<utc>-<hex8>:<subtask>:1`.
- Canaries: after each real delegation, when the count of real ledger rows for (agent, skill)
  reaches a multiple of M (default 10) and fewer canaries than due were sent, the broker sends
  one canary in the same format. Its ledger row (`kind = "canary"`) counts in reliability;
  success = completed, spot check passed and graded passed. Two consecutive canary failures
  -> SOFT `CANARY_FAIL`.
- Amendment to ADR-0030: graders may grade exam-bank outputs (truth we generated); benchmark
  truth never feeds reliability or admission.
- `bourse stack activate` now requires `--all --force-unsafe` and audits
  "manual activation bypassing admission exam". ADR-0028 is superseded.

## Consequences
- An agent can be ACTIVE only by passing the exam or by an audited unsafe override.
- Exams and canaries spend real LLM tokens; canaries add 1 delegation per M.

## Status
Accepted (Phase 5a).
