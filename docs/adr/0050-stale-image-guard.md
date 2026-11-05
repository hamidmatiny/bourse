# 0050 — Stale-image guard on compose stack up

## Context
Every BOURSE compose service runs one image, `bourse:local` (ADR-0034). `docker compose up`
reuses an existing image even when `src/` or the Dockerfile changed after it was built, so a
live acceptance run can exercise old code while the host tree has the fix.

## Decision
- `bourse stack up --mode compose` checks, before compose up, every image the compose document
  builds (services with `build:`). For each, the inputs are the Dockerfile plus the sources of
  its `COPY`/`ADD` lines (stage copies `--from=` excluded), minus `.dockerignore` matches and
  cache directories.
- If any input file's mtime is newer than the image's `Created` time, stack up exits 1 and
  prints each stale image, the newest input file, both times, and `run \`docker compose build\``.
- A missing image is not stale (compose builds it on up).
- Module `bourse.stack.image_freshness`; pure logic (`find_stale`) is unit-tested with injected
  image times.

## Consequences
- A run after editing code without rebuilding is refused in under a second instead of
  producing results from old code.
- mtime is conservative: a checkout that touches files without changing content also asks
  for a rebuild.
- `--mode host` is unaffected.

## Status
Accepted (Phase 5c).
