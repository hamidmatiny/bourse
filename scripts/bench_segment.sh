#!/bin/sh
# One BOURSE-Bench segment on a fresh stack (ADR-0053):
#   stack down -> stack up (flags) -> [bench preflight] -> bench launch [--final]
# Usage: scripts/bench_segment.sh dev|final <config.yaml> [stack up flags...]
# Prints "=== SEGMENT <name> exit <code>" at the end; never stops a whole night on one failure.
set -u
kind="$1"
cfg="$2"
shift 2
name=$(basename "$cfg" .yaml)
echo "=== SEGMENT $name start $(date -u +%Y-%m-%dT%H:%M:%SZ) kind=$kind stack-up: $*"
uv run bourse stack down
if ! uv run bourse stack up --mode compose "$@"; then
    echo "=== SEGMENT $name exit 10 (stack up failed) $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 10
fi
if [ "$kind" = "final" ]; then
    if ! uv run bourse bench preflight --config "$cfg"; then
        echo "=== SEGMENT $name exit 11 (bench preflight refused) $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        exit 11
    fi
    uv run bourse bench launch --final --config "$cfg"
else
    uv run bourse bench launch --config "$cfg"
fi
code=$?
echo "=== SEGMENT $name exit $code $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit "$code"
