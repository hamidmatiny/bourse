# 0049 — Exam INFRA attribution and global exam concurrency

## Context
Admission exams for 5 agents were scheduled concurrently. With `OLLAMA_NUM_PARALLEL=2`, the
meter returned 429 `contention` (queue_wait 52–60 s). Agents surfaced that as
`llm_unavailable: AllProvidersFailed` or `deadline: DeadlineExceeded`. ExamRunner counted those
as agent failures toward n/N and recorded `EXAM_FAILED`, so honest agents stayed PENDING despite
zero grader failures.

Canaries already treat timeouts / 429 / CONTENTION as reliability/infra, never trust strikes
(ADR-0039 / ADR-0042 fix round 2). Exams need the same fault attribution.

## Decision
- **INFRA** exam-task failures (not agent fault): meter contention / 429; `error_kind=llm_unavailable`
  (429/5xx-derived provider failure); `deadline` when `queue_wait_ms > contention_queue_fraction`
  (default 0.5) of the task deadline. Detect via A2A `error_kind`/`error` text and optional
  `llm_usage` lookup for the task_id.
- INFRA slots retry up to 2 times with linear backoff. Only grader failures and other
  agent-attributable errors count toward n/N. Pass mark and k are unchanged (still need
  `pass_n`/`k` per skill).
- If any slot remains INFRA after retries: outcome `EXAM_INCONCLUSIVE` — agent stays PENDING,
  SOFT `EXAM_INCONCLUSIVE` violation (not `EXAM_FAILED`), exam rescheduled after a short delay.
  Cooldown applies only to `EXAM_FAILED`.
- Agent-side error kinds for the openai SDK (CrewAI's LLM client, `pii-precise`): 429 without
  billing/quota wording, 5xx, connection errors and client timeouts map to `llm_unavailable`
  (INFRA), not `internal`. 401/402/403 and billing/quota bodies map to `provider_auth`, which is
  not INFRA (breaker class, AGENTS.md rule 6). The exception chain (`__cause__`/`__context__`)
  is walked so wrapped errors map the same way.
- **ExamScheduler** global concurrency across agents = `OLLAMA_NUM_PARALLEL` (config); excess
  exams wait in a printed queue. Per-agent task concurrency remains 1.
- `wait-active` / exam summary prints per agent: passed / grader-failed / infra-retried /
  inconclusive counts.

- **Hard deadlines (Phase 5c amendment).** Each exam attempt (meter budget register + A2A
  send/await) runs under one `asyncio.timeout(task_deadline + slot_grace_s)`. Expiry, a meter
  error (`MeterUnavailable`) or an exhausted exam wall are INFRA: retried, then
  EXAM_INCONCLUSIVE. ExamScheduler wraps each exam in a backstop cap
  (`exam_timeout_s + task_deadline + 60s`) that records EXAM_INCONCLUSIVE if a non-A2A await
  stalls. `send_task` itself is capped at `deadline_s + 30s` for callers without their own
  cap; in-flight CancelTask is capped at 10s.
- **Progress.** `GET /exams/progress` returns `ExamProgress` (agent, skill, task n/N, attempt,
  task and exam elapsed, queued exams). wait-active prints a line on every task/attempt change
  and all running exams on each heartbeat. Exam task lines (`exam start`, `exam task ...
  outcome=...`, `exam done`) go to registry stdout, so compose logs and diagnostics show them.
- **wait-active budget** is computed from task counts: per exam
  `min(tasks * (1 + retries) * (deadline + grace), hard cap)`; makespan bound
  `sum / OLLAMA_NUM_PARALLEL + max + 120s`. Honest roster (5 exams, 30 tasks, deadline 300s):
  4530s. It replaces the fixed `exam_timeout_s + 120` = 1020s.
- Observed (2026-11-05 compose run) that motivated the amendment: registration 19:56:51;
  exams ran 2 at a time; pii-precise exam 19:56:51-20:11:03 (852s of the 900s exam wall);
  reporter queued until 20:07:59, then 5/5 tasks (6 LLM calls, all HTTP 200), ACTIVE at
  20:15:05 = 1094s after registration. wait-active gave up at 1020s while the exam was still
  running. The exam did not hang; the budget was too small and progress was invisible
  (exam task lines were logged at INFO with no handler, so diagnostics printed
  "no exam task lines yet").

## Amendment (Phase 6b, 2026-11-06): an exam that cannot pass is EXAM_FAILED
- **Observed (dev6b-no-metercap):** A8a-raw (token burner) and A8b-raw (slow loris) failed
  every exam task on their own 300 s task deadline. These are agent-attributable ERRORs
  (queue wait 0). After 3 tasks the 900 s exam wall ran out, so the last 2 slots were INFRA
  (EXAM_TIMEOUT). INFRA left meant INCONCLUSIVE, even though 3 failures already made 4/5
  impossible. The agent stayed PENDING, the exam was rescheduled, and the phase5 runner
  waited the honest roster's 4530 s budget for each one (4603 s and 4717 s measured).
- **Rule:** once one skill has more agent-attributable failures (grader-failed or ERROR) than
  `k - pass_n`, no retry or reschedule can make the exam pass. The exam stops and is
  EXAM_FAILED. INFRA still gives INCONCLUSIVE only while passing is still possible.
- **Effect:** with k=5 and pass_n=4 the exam ends at the 2nd failure. Honest agents get the
  same verdict as before, only sooner.
- **Adversary exam wait (phase5 runner):** the runner waits the adversary's own budget
  (`exam_wait_s` for k x its skills, 2010 s for one skill), not the honest roster's 4530 s.
  An EXAM_INCONCLUSIVE violation ends the wait at once, and the row is an infra error
  (ADR-0053 amendment 1, rule R1).
- Tests: `test_exam_that_cannot_pass_is_failed_not_inconclusive` (the dev6b case),
  `test_infra_with_too_few_failures_stays_inconclusive`.

## Amendment 2 (Phase 6b, 2026-11-07): attribute exam failures from meter evidence
- **Observed (reval6b):** after amendment 1, three rows still ended as "error: exam
  inconclusive": full-stack A8b-raw, and no-metercap A8a-raw and A8b-raw. Every exam task timed
  out, and *a task timeout was always INFRA* (`EXAM_TIMEOUT: task deadline 300s`). So no
  agent-attributable failure was ever counted, and amendment 1 never applied. Registry log,
  no-metercap: both agents 0/5, 3 timeouts, then 4 "exam wall budget exhausted", elapsed 905 s.
  Meter rows for the same task ids:
  - token-burner-raw: about 53 calls (117 tokens each, about 5.6 s each) before the 300 s
    deadline, against a task budget of 20 calls. Nothing refused it. Each task went on to
    1498–1581 calls in total, because the agent ignores the A2A cancel.
  - slow-loris-raw: 0 calls.
- **Decision:** one attribution function, `bourse.trust.exam_fault.attribute_exam_failure`, used
  for every failed or timed-out exam task. `is_exam_infra_failure` is now a thin view over it.
  The first rule that applies decides:

  | rule | fault | when |
  |---|---|---|
  | A1 | agent | the meter refused this task: budget_exhausted / task_budget_exceeded |
  | I1 | infra | the meter recorded 429 (not a budget refusal), 5xx, contention, breaker_open or upstream failure for this task |
  | I2 | infra | meter queue wait > contention_queue_fraction × task deadline |
  | I3 | infra | (failed task) the agent reported llm_unavailable / contention / HTTP 5xx |
  | A2 | agent | (failed task) any other failure |
  | I4 | infra | (timeout) no meter status for the task: meter health unproven |
  | A3 | agent | (timeout) calls or tokens beyond the task budget |
  | I5 | infra | (timeout) an LLM call of this task was inside the meter at the deadline |
  | A4 | agent | (timeout) meter healthy, every LLM call of the task completed (stall) |

  Agent fault means the task is ERROR: it counts toward n/N, and with amendment 1 the 2nd one
  makes the exam EXAM_FAILED. Infra means INFRA: retried up to infra_max_retries, then
  INCONCLUSIVE.
- **Meter evidence:** the meter keeps, per task, the requests currently inside it (gate queue
  plus upstream), the admitted calls and tokens, and the budget refusal codes. These are
  served at `GET /budgets/status?task_id=` (broker key only, contract `TaskBudgetStatus`).
  In-flight counting runs in every stack profile. Under no-metercap nothing is refused, but
  calls and tokens are still counted against the registered ceiling, so A3 still applies.
- **Fix found on the way:** before this amendment, a budget refusal row (429, error_kind
  BUDGET) counted as contention, which is infra. A refused token burner on the full stack was
  therefore INFRA. Under rule A1 it is now agent fault.
- **Record:** every `ExamTaskOutcome` carries `fault` (agent / infra), `fault_rule` (A1–I5) and
  `evidence`. The evidence string holds the meter numbers, e.g. `calls=25/20 tokens=…/8192
  in_flight=1 rows=… queue_wait_ms=…`. The `exam task` stdout line prints all three.
- **Known limit, kept:** I3 still trusts an agent's own infra report. A false claim can only
  make the exam INCONCLUSIVE, never passed.
- **Tests:**
  - `tests/unit/test_exam_fault.py`: one or more tests per rule, plus rule order and the
    evidence string.
  - `tests/unit/test_exam_fault_hermetic.py`: the real meter app and the real ExamRunner with
    the real MeterBudgetClient and usage lookup. The scripted results:

    | agent | result | rule |
    |---|---|---|
    | slow-loris | EXAM_FAILED | A4 |
    | token burner, no-metercap | EXAM_FAILED | A3 |
    | token burner, hard cap | EXAM_FAILED | A1 |
    | honest, first upstream call 503 (meter records 502 upstream_failed) | 1 INFRA retry, then passed and ACTIVE | I1 |
    | honest, persistent 5xx | INCONCLUSIVE, no EXAM_FAILED | I1 |

  - In that test each scripted meter call runs as its own task. In the stack the meter is a
    separate process and keeps serving after the agent's task is cancelled. In process, the
    exam's cancel would reach into the meter's fair-share gate (observed: the loop then did not
    end).

## Consequences
- Contention during exams no longer permanently fails honest agents.
- Exams take longer wall time when the queue backs up, but finish with agent-attributable evidence.
- Registry process needs read access to `llm_usage` (same Postgres) for deadline/queue_wait checks.

## Status
Accepted (Phase 5c). Amended Phase 6b: amendment 1 (exam that cannot pass), amendment 2
(fault attribution from meter evidence).
