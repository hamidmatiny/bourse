# 0026 — Delegation protocol: card-drift check, attempts, fallbacks, contract validation

## Context
The registry stores the card revision it verified. The agent host can serve a different card
later. Agents fail, time out, return malformed artifacts or ask for input.

## Decision
- Before every attempt the broker fetches the live card from the card_url on record (64 KiB cap,
  5 s deadline, no redirects: the registry fetcher) and compares its JCS payload digest with the
  registry's current revision digest. On mismatch: no A2A message is sent to that agent, a SOFT
  `CARD_DRIFT` violation is posted to `POST /agents/{id}/violations` (admin token), a
  `POST /agents/{id}/refresh` is triggered, and the next candidate is tried. The A2A client is
  built from the fetched card object, so the verified card is the one used.
- Message: one DataPart = the SKILL_IO input built from the dataset and the validated outputs of
  the subtask's ancestors; metadata `{skill_id, task_id = run_id:subtask_id:attempt,
  deadline_s}`; streaming client.
- Per-delegation deadline: the agent gets `deadline_s` (default 300); the broker wraps the call in
  a wall-clock deadline of deadline_s + 15 s.
- Outcomes per attempt (one append-only `delegations` row each): COMPLETED, FAILED, REJECTED,
  TIMEOUT, INTERRUPTED (INPUT_REQUIRED / AUTH_REQUIRED; Phase 5 adds a policy), FAILED_CONTRACT
  (no artifact, or artifact not the SKILL_IO output model), CARD_DRIFT, ERROR (card fetch or
  transport failure).
- Fallback: any outcome other than COMPLETED moves to the next ranked candidate; at most 2
  attempts per subtask (a CARD_DRIFT attempt counts). Then the subtask is FAILED, its dependents
  are SKIPPED without delegation, and the run is FAILED with completed outputs kept.
- Breaker: checked before every attempt. An agent that fails with `llm_unavailable: BreakerOpen`
  trips the broker's global breaker; the run is ABORTED, in-flight delegations of the level are
  cancelled and no further delegation starts.
- Independent subtasks of one dependency level run concurrently through
  `Executor.submit_llm` (semaphore size OLLAMA_NUM_PARALLEL). The Phase 3 executor implements
  only submit_llm; the CPU pool stays in Phase 6.
- The whole run is under a watchdog (wall budget, default 1800 s; heartbeat on every delegation
  row; timeout deadline_s + 15 s + 60 s). A watchdog kill cancels the run and marks it ABORTED.

## Consequences
- A drifted agent receives 0 POST requests (unit-tested).
- The broker holds the registry admin token in Phase 3 to report violations; Phase 5 moves
  violation reporting to the trust runtime.

## Status
Accepted (Phase 3).
