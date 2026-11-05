#!/usr/bin/env bash
# Phase 5b acceptance: unit/db tests, compose stack with adversaries only after
# honest agents are ACTIVE, phase5 attacks (background + progress), scores, compose tests.
# Does not commit. Stop on any failure; dump exam/llm_usage diagnostics on wait-active fail.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ATTACK_LOG="${ATTACK_LOG:-data/logs/attacks5.log}"
STACK_LOG="${STACK_LOG:-data/logs/stack-up-5b.log}"
ATTACK_POLL_S="${ATTACK_POLL_S:-15}"

echo "==> stack down (clean slate)"
uv run bourse stack down

echo "==> unit tests"
uv run pytest

echo "==> db tests (postgres only, then tear down)"
docker compose up -d postgres
uv run pytest -m db
uv run bourse stack down

echo "==> compose build"
docker compose build

echo "==> probe --latency 5 (writes data/probe/latency-latest.json; exam deadline = max(base, 3*p95))"
uv run bourse probe --latency 5

echo "==> note: compose llm_usage is in database bourse_compose (migrate), not POSTGRES_DB=bourse"
echo "==> stack up --with-adversaries (register + wait-active, then adversaries)"
mkdir -p "$(dirname "$STACK_LOG")" "$(dirname "$ATTACK_LOG")"
# On wait-active / register failure, stack up dumps diagnostics and exits non-zero.
if ! uv run bourse stack up --mode compose --with-adversaries 2>&1 | tee "$STACK_LOG"; then
  echo "stack up --with-adversaries FAILED" >&2
  echo "Query llm_usage with: docker compose exec postgres psql -U bourse -d bourse_compose -c \"select agent_id,status,error_kind,count(*),max(queue_wait_ms) from llm_usage group by 1,2,3 order by 1;\"" >&2
  exit 1
fi

: >"$ATTACK_LOG"

echo "==> phase5 attacks (background -> $ATTACK_LOG)"
# shellcheck disable=SC2094
docker compose run --rm broker bourse attacks phase5 --runs 6 >"$ATTACK_LOG" 2>&1 &
ATTACK_PID=$!
START_TS="$(date +%s)"
echo "attacks pid=$ATTACK_PID log=$ATTACK_LOG"

# Wait with progress + ETA parsed from `[i/N] sid eta=…` lines when present.
while kill -0 "$ATTACK_PID" 2>/dev/null; do
  ELAPSED=$(( $(date +%s) - START_TS ))
  LAST="$(tail -n 1 "$ATTACK_LOG" 2>/dev/null || true)"
  ETA_MSG=""
  if [[ "$LAST" =~ \[([0-9]+)/([0-9]+)\][[:space:]]+([^[:space:]]+)[[:space:]]+eta=([0-9]+)s ]]; then
    I="${BASH_REMATCH[1]}"
    N="${BASH_REMATCH[2]}"
    SID="${BASH_REMATCH[3]}"
    ETA="${BASH_REMATCH[4]}"
    ETA_MSG=" [${I}/${N}] ${SID} eta=${ETA}s"
  fi
  echo "[attacks ${ELAPSED}s]${ETA_MSG} ${LAST:0:100}"
  sleep "$ATTACK_POLL_S"
done

set +e
wait "$ATTACK_PID"
ATTACK_RC=$?
set -e
ELAPSED=$(( $(date +%s) - START_TS ))
echo "attacks finished rc=$ATTACK_RC after ${ELAPSED}s"
tail -n 40 "$ATTACK_LOG" || true
if [[ "$ATTACK_RC" -ne 0 ]]; then
  echo "phase5 attacks FAILED (rc=$ATTACK_RC); full log: $ATTACK_LOG" >&2
  exit "$ATTACK_RC"
fi

echo "==> scores (compose mode -> bourse_compose)"
uv run bourse scores

echo "==> compose tests (clean stack, no adversaries)"
uv run bourse stack down
uv run bourse stack up --mode compose
uv run pytest -m compose -q -rA

echo "==> acceptance summary"
echo "--- exams (from stack up / wait-active) ---"
grep -E '^exams resident peak ' "$STACK_LOG" || echo "exams resident peak: (not found in $STACK_LOG)"
echo "--- attacks (from phase5) ---"
grep -E '^attacks resident peak ' "$ATTACK_LOG" || echo "attacks resident peak: (not found in $ATTACK_LOG)"
grep -E '^model reloads ' "$ATTACK_LOG" || echo "model reloads: (not found in $ATTACK_LOG)"
if grep -qE '^abort_reason ' "$ATTACK_LOG"; then
  grep -E '^abort_reason ' "$ATTACK_LOG"
else
  echo "abort_reason (none)"
fi

echo "==> stack down"
uv run bourse stack down

echo "acceptance_5b DONE"
echo "Next (operator): git add -A && gitleaks protect --staged --no-banner --redact && git status --short"
