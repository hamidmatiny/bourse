# 0024 — Planner: submit-tool over the closed taxonomy, template fallback, no card text in prompts

## Context
The broker turns an operator request into typed subtasks. An LLM can read intent from free text
but can also return invalid graphs, and any text it reads can steer it. Agent Cards and registry
data are written by vendors and are untrusted (ADR-0008).

## Decision
- Input contract `IntakeRequest(dataset: DatasetRef, request_text: str <= 1000, budget)`. This
  replaces the Phase 0 placeholder shape (request_id, dataset_uri, requested_skills,
  submitted_at), which nothing used.
- One LLM (the large model) through the metering proxy with the broker's own meter key and
  `X-Bourse-Task-Id = run_id`. The prompt contains exactly: `request_text` and `SKILL_TAXONOMY`,
  which is written in `broker/planner.py`. It does not reuse the agents' card skill text, and no
  registry or card field is passed to the planner at all.
- The model submits `PlanSubmission(subtasks: [{id, skill, depends_on}])`; the broker sets
  `job_id = run_id` and `input_ref = "dataset"` when building the `TaskDAG`.
- Validation inside `PlanSubmission`, so errors are fed back by the submit-tool loop: `TaskDAG`
  graph checks (duplicate ids, unknown dependencies, cycles), SkillId enum (unknown skills), and
  policy: each SkillId at most once; must contain report.compose; report.compose depends
  directly on the schema.profile and stats.compute subtasks.
- 3 attempts (first answer + 2 retries). On SubmitFailed or any LLM failure (provider error,
  breaker, budget, deadline) the deterministic template plan is used: profile; stats (depends on
  profile); pii; units; report (depends on all four). `planner_used` is "llm" or "template".

## Consequences
- A unit test registers cards whose description holds a unique sentinel string, runs a full
  plan + delegation, and asserts the sentinel is in 0 LLM request bodies.
- The report only receives outputs of subtasks it depends on (directly or transitively).
- An LLM plan may omit pii.detect or units.normalize; that is allowed by the policy.

## Status
Accepted (Phase 3).
