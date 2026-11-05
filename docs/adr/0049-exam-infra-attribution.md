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

## Consequences
- Contention during exams no longer permanently fails honest agents.
- Exams take longer wall time when the queue backs up, but finish with agent-attributable evidence.
- Registry process needs read access to `llm_usage` (same Postgres) for deadline/queue_wait checks.

## Status
Accepted (Phase 5c).
