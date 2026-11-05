#!/usr/bin/env bash
# Phase 5c acceptance (ADR-0048): one live run, resumable by stage. Does not commit.
#
#   bash scripts/acceptance_5c.sh                  # all stages: gate .. finish
#   bash scripts/acceptance_5c.sh --from STAGE     # resume at STAGE (stack must be up for
#                                                  # a5a and later; nothing is torn down)
#   bash scripts/acceptance_5c.sh --teardown       # only `bourse stack down`
#
# Stages, in order:
#   gate     lint + unit + db tests incl. the hermetic 5c state machine (no Slack, no Ollama)
#   build    docker compose build (fresh image; stack up refuses stale images, ADR-0050)
#   up       bourse stack up --mode compose --with-adversaries (registers, exams, 5/5 ACTIVE)
#   a5a      A5a sole-provider: sleeper caught and QUARANTINED
#   slack    approve test: timeout (do not click) then approve (click APPROVE)
#   deny     reinstate exam pass -> click Deny -> stays QUARANTINED
#   approve  reinstate exam pass -> click APPROVE -> ACTIVE, rel_epoch N -> N+1
#   recatch  same identity (--reuse-agent): caught again with k strikes in the new epoch
#   finish   stack down + gitleaks protect --staged
#
# On any failure the script stops and leaves the stack up; it prints the --from command to
# resume. The stack is torn down only after every stage passed, or with --teardown.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAGES=(gate build up a5a slack deny approve recatch finish)
AGENT_ID="${AGENT_ID:-sleeper-loud@vendor-x.local}"
SLEEPER_PORT="${SLEEPER_PORT:-55503}"  # A5a = ADVERSARIES index 2 -> 55501+2
SLEEPER_SVC="${SLEEPER_SVC:-agent-sleeper-loud}"
LOGDIR="data/logs/acceptance5c"
REGISTRY="http://127.0.0.1:55300"
APPROVER="http://127.0.0.1:55310"
mkdir -p "$LOGDIR"

FROM="gate"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --from=*) FROM="${1#--from=}"; shift ;;
    --teardown) uv run bourse stack down; exit 0 ;;
    *) echo "unknown arg: $1 (use --from STAGE or --teardown)" >&2; exit 2 ;;
  esac
done
START=-1
for i in "${!STAGES[@]}"; do [[ "${STAGES[$i]}" == "$FROM" ]] && START=$i; done
if [[ $START -lt 0 ]]; then
  echo "unknown stage '$FROM'; one of: ${STAGES[*]}" >&2
  exit 2
fi

CURRENT=""
on_error() {
  echo "" >&2
  echo "!!! stage '${CURRENT}' FAILED. Stack left up (no teardown)." >&2
  echo "!!! fix, then resume: bash scripts/acceptance_5c.sh --from ${CURRENT}" >&2
  echo "!!! clean up instead:  bash scripts/acceptance_5c.sh --teardown" >&2
}
trap on_error ERR

fail() { echo "FAIL [${CURRENT}]: $*" >&2; return 1; }

banner() {
  echo ""
  echo "########################################################################"
  for line in "$@"; do echo "#  $line"; done
  echo "########################################################################"
}

agent_field() {  # agent_field state|rel_epoch
  curl -sf "${REGISTRY}/agents/${AGENT_ID}" | uv run python -c "
import sys, json
a = json.load(sys.stdin)
if sys.argv[1] == 'state':
    print(a.get('state', '?'))
else:
    tags = [t for t in a.get('tags') or [] if t.startswith('rel_epoch:')]
    print(tags[0].split(':', 1)[1] if tags else 0)
" "$1" 2>/dev/null || echo "MISSING"
}

honest_active_n() {
  curl -sf "${REGISTRY}/agents?state=ACTIVE&limit=200" | uv run python -c "
import sys, json
print(sum(a.get('vendor_domain') in ('vendor-a.local','vendor-b.local','vendor-c.local')
          for a in json.load(sys.stdin)))
" 2>/dev/null || echo 0
}

require_stack() {
  curl -sf "${REGISTRY}/agents" >/dev/null 2>&1 \
    || fail "registry unreachable; run: bash scripts/acceptance_5c.sh --from up"
  local n; n="$(honest_active_n)"
  [[ "$n" == "5" ]] || fail "honest ACTIVE ${n}/5; run: bash scripts/acceptance_5c.sh --from up"
}

require_state() {  # require_state QUARANTINED|ACTIVE
  local st; st="$(agent_field state)"
  [[ "$st" == "$1" ]] || fail "${AGENT_ID} state=${st}, need $1"
}

reset_sleeper() {
  echo "==> /bourse/reset-behavior on ${SLEEPER_SVC} (reinstate exam can pass)"
  docker compose exec -T "$SLEEPER_SVC" \
    curl -sf -X POST "http://127.0.0.1:${SLEEPER_PORT}/bourse/reset-behavior"
  echo
}

run_logged() {  # run_logged LOGNAME cmd...  -> rc of cmd; output tee'd
  local log="$LOGDIR/$1.log"; shift
  set +e
  "$@" 2>&1 | tee "$log"
  local rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

stage_gate() {
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy --strict src tests
  uv run pytest
  docker compose up -d postgres
  for _ in $(seq 1 30); do docker compose exec -T postgres pg_isready -q && break; sleep 1; done
  uv run alembic upgrade head
  uv run pytest -m db
  uv run bourse stack down
}

stage_build() { docker compose build; }

stage_up() { uv run bourse stack up --mode compose --with-adversaries; }

stage_a5a() {
  require_stack
  local st; st="$(agent_field state)"
  if [[ "$st" == "QUARANTINED" ]]; then
    echo "==> ${AGENT_ID} already QUARANTINED; A5a done"
    return 0
  fi
  run_logged a5a docker compose run --rm broker bourse attacks phase5 --only A5a \
    --mode sole-provider || fail "phase5 A5a rc=$?"
  require_state QUARANTINED
}

stage_slack() {
  require_stack
  local slack
  slack="$(curl -sf "${APPROVER}/healthz" | uv run python -c \
    "import sys,json; print(json.load(sys.stdin).get('slack'))" 2>/dev/null || echo unreachable)"
  [[ "$slack" == "connected" ]] || fail "approver slack=${slack}"
  banner "SLACK 1/2: TIMEOUT TEST - DO NOT CLICK" "message must turn EXPIRED after 30s"
  if run_logged slack-timeout uv run bourse approve test --require-slack --timeout 30 \
      --expect none; then
    fail "timeout test approved (expected reason=timeout)"
  fi
  grep -q "reason=timeout" "$LOGDIR/slack-timeout.log" || fail "no reason=timeout"
  banner "SLACK 2/2: CLICK APPROVE" "use the button whose request_id matches the banner below"
  run_logged slack-approve uv run bourse approve test --require-slack --timeout 900 \
    --expect approve || fail "approve test not approved"
}

stage_deny() {
  require_stack
  require_state QUARANTINED
  reset_sleeper
  banner "REINSTATE DENY: after the exam passes, CLICK DENY (and confirm)" \
    "request_id is printed in the next banner"
  if run_logged deny uv run bourse trust reinstate "$AGENT_ID" --reason "5c deny path" \
      --expect deny; then
    fail "deny path returned ACTIVE"
  fi
  grep -q "status=denied" "$LOGDIR/deny.log" || fail "no status=denied"
  grep -q "action=deny" "$LOGDIR/deny.log" || fail "decision was not a Deny click"
  require_state QUARANTINED
}

stage_approve() {
  require_stack
  require_state QUARANTINED
  local before; before="$(agent_field rel_epoch)"
  reset_sleeper
  banner "REINSTATE APPROVE: after the exam passes, CLICK APPROVE" \
    "request_id is printed in the next banner"
  run_logged approve uv run bourse trust reinstate "$AGENT_ID" --reason "5c approve path" \
    --expect approve || fail "approve path did not reach ACTIVE"
  require_state ACTIVE
  local after; after="$(agent_field rel_epoch)"
  [[ "$after" == "$((before + 1))" ]] || fail "rel_epoch ${before} -> ${after}"
  echo "==> rel_epoch ${before} -> ${after}"
}

stage_recatch() {
  require_stack
  require_state ACTIVE
  local epoch; epoch="$(agent_field rel_epoch)"
  [[ "$epoch" -ge 1 ]] || fail "rel_epoch=${epoch}; recatch needs a reinstated sleeper"
  run_logged recatch docker compose run --rm broker bourse attacks phase5 --only A5a \
    --mode sole-provider --reuse-agent || fail "phase5 re-catch rc=$?"
  local log="$LOGDIR/recatch.log"
  grep -q "invariants A5a/sole-provider: ok" "$log" || fail "invariants not ok"
  grep -Eq "strikes=([3-9]|[0-9]{2,}) rel_epoch=${epoch}" "$log" \
    || fail "fewer than k=3 strikes in epoch ${epoch}"
  grep -Eq "first_bad=[0-9]+ detect_delay=[0-9]+" "$log" \
    || fail "first_bad/detect_delay unresolved"
  require_state QUARANTINED
}

stage_finish() {
  uv run bourse stack down
  git add -A
  gitleaks protect --staged
}

for ((i = START; i < ${#STAGES[@]}; i++)); do
  CURRENT="${STAGES[$i]}"
  echo ""
  echo "==================== stage ${CURRENT} ($((i + 1))/${#STAGES[@]}) ===================="
  "stage_${CURRENT}"
  echo "==> stage ${CURRENT} ok"
done
trap - ERR
echo ""
echo "==> acceptance_5c: all stages ok (not committed). logs: ${LOGDIR}/"
grep -hE "A5a/sole-provider: exposure|rel_epoch [0-9]+ -> |exam passed|reinstate status=" \
  "$LOGDIR"/*.log || true
