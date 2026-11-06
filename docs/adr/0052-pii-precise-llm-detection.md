# 0052 — pii-precise: LLM column-kind and free-text detection on top of the deterministic detectors

## Context
Dev pilot `pilot-6a` (2026-11-05) sent the same 5 hard_pii dev cases (dev-0005, -0011, -0017,
-0023, -0029) directly to both PII agents.

| agent | passed | F1 per case | wall per case |
|---|---|---|---|
| vendor-c-pii-fast | 0/5 | 0.769, 0.830, 0.836, 0.842, 0.800 | 6.3-7.1 s |
| vendor-b-pii-precise | 0/5 | 0.769, 0.830, 0.836, 0.842, 0.800 | 63.7-92.8 s |

Pass threshold: `PII_F1_THRESHOLD = 0.95` per case (`grading/graders.py`, GRADER_VERSION 1).
The 0.830 in the summary is the median of the five F1 values.

Why the two agents tie: both run `regex_findings` (MCP regex_scan + Luhn + SIN check), the same
deterministic step. They differ only in how they choose PERSON_NAME columns (pii-fast: one submit
call on 3 samples per column; pii-precise: a CrewAI crew on 5 sample rows). Every one of the 5
pilot cases has the `split_name` and `word_name` features, and both agents chose exactly
`first`, `last`, `contact_ref`. For each case, that is the only name-column set that gives
the recorded tp/fp/fn (checked offline by enumerating every subset of the columns the regex
did not flag, with the deterministic step replayed). So the only step where the agents differ
had no errors on these 5 cases.

Byte-identity, observed: re-run `pii-identity-6a` (2026-11-06, same 5 cases, records with
`output_sha256`) gives byte-identical outputs on 5/5 cases (Wilson [0.57, 1.00]). The
tp/fp/fn values match the pilot on every case. Wall per case in this run: pii-fast
11.3-12.2 s, pii-precise 112.4-168.5 s, slower than the pilot (6.3-7.1 s and 63.7-92.8 s).
Both agents' timings are reported. The wall estimate below uses the pilot's numbers.

What both miss (all from the shared deterministic step; counts per case):

| case | missed (fn) | false positives (fp) |
|---|---|---|
| dev-0005 | `reach` obfuscated email 10, `tel` international phone 10, `notes` phone in text 4 | 0 |
| dev-0011 | `notes` phone in text 19 | `sku` NANP-shaped product code 30 |
| dev-0017 | `reach` 21, `notes` 12 | 0 |
| dev-0023 | `tel` 30, `notes` 15 | 0 |
| dev-0029 | `reach` 17, `tel` 17 | 0 |

`classify` uses `fullmatch`, so a phone inside "call 555-… after 5pm" is not a candidate. "x [at]
mail [dot] com" and "+44 …" do not match the EMAIL / NANP PHONE patterns. The `color`,
`ref_no` (Luhn-invalid) and `batch` (SIN-invalid) decoys were rejected correctly; the `sku`
decoy was not.

Cases, generator and grader are not changed by this ADR.

## Decision (as proposed; see the amendment for what was approved and built)
pii-precise keeps the deterministic step and adds two LLM steps (submit-tool pattern,
ADR-0006, `think: false`, same meter and task budget). pii-fast is unchanged.

1. **Column-kind call (one call per task).** The input is the header plus 5 sample values for
   two kinds of column: those the regex did not flag and were not chosen as name columns, and
   those flagged PHONE. Output per column: `EMAIL | PHONE | FREE_TEXT_CONTACT | NOT_PII`.
   - EMAIL / PHONE on an unflagged column marks every non-empty cell (obfuscated emails,
     international phones).
   - NOT_PII on a regex-PHONE column drops that column's PHONE findings (NANP-shaped codes).
   - The LLM can only add or remove findings for whole columns. It cannot change CARD or SIN
     findings: those stay Luhn/SIN-checked.
2. **Free-text call (one call per FREE_TEXT_CONTACT column).** All values of the column (at most
   30 rows in bench cases) with row indices. Output: the row indices whose value contains a
   phone or email, and the kind. Indices outside the table are rejected by the Pydantic model.

Upper bound if every LLM label were correct (replayed against the 5 pilot cases' truth):
- step 1 only: dev-0005 F1 0.968, dev-0023 0.952 and dev-0029 1.000 pass; dev-0011 0.927 and
  dev-0017 0.946 fail. That is 3/5.
- steps 1 + 2: 5/5.
- pii-fast stays 0/5.

The real values come from a pilot. The LLM labels can also be wrong, which would lower recall
or precision. This is an upper bound, not a prediction.

### Cost estimate (not measured)
- Wall: pii-fast's whole task, regex plus one submit call on samples, took 6.3-7.1 s. One
  similar call is about 7 s. Step 1 adds 1 call; step 2 adds 0-1 calls on these cases (only
  the `notes` column). Estimate: +7-15 s on the 63.7-92.8 s pii-precise task now takes, about
  +10-20%.
- Tokens: there is no per-agent token count for the PII-only path in the pilot. B2 always used
  pii-precise and B3 always used pii-fast, and both ran the same other four agents. The B2 − B3
  difference in whole-job tokens p50 is 10014 − 4680 = 5334. So pii-precise's crew uses about
  5.3k tokens more than pii-fast per task. Two more calls of about 1-2k tokens each could push
  pii-precise past the per-task budget `BOURSE_TASK_MAX_TOKENS` = 8192 (ADR-0041). The budget
  is not raised by this ADR. If the pilot shows budget failures, that is a separate decision.
- Selection effect: B3 picks by reliability − cost − latency. A slower, costlier pii-precise
  is picked only if its measured reliability on hard cases outweighs the cost. That trade-off
  is what the comparison is meant to measure.

### Measurement
- Run the dev pilot again on the same 5 hard_pii cases plus 5 more dev hard_pii cases. Report
  passed n/N, F1 per case, wall, tokens, task-budget failures, and byte-identical n/N.
- No test-split case is used.

## Amendment: approved with corrections, implemented (fix round 2, 2026-11-06)
Operator approval came with three corrections. The implementation follows the corrections,
not the proposal above where they differ:
- **Union only.** The LLM only adds findings. The NOT_PII veto on regex-PHONE columns
  (proposal step 1) is dropped, so deterministic findings are never removed, including the
  NANP-shaped `sku` decoy.
- **Deterministic decoy filter on every LLM proposal.** New MCP tool `contact_check(values,
  kinds)` in `mcp_servers/pii.py`:
  - EMAIL must fully match the regex_scan email pattern after de-obfuscating `[at]`, `(at)`,
    `{at}`, `<at>` and ` at ` (same for dot), case-insensitive;
  - PHONE is a NANP number anywhere in the text, or `+` country code then digit groups with
    8-15 digits in total;
  - CARD must be Luhn-valid and SIN must pass the SIN check.

  See "Validity threat" below.
- **Card 1.1.0.** The card is re-signed at agent start with the vendor-b key and must pass the
  admission exam again on a fresh stack. The MCP allowlist for pii-precise gains
  `contact_check`.
- **Bounded steps.** Step 1 is one submit call with `max_tokens` 512 and 2 attempts. Step 2 is
  one call per FREE_TEXT column, at most 3 columns and 200 rows, with `max_tokens` 768.
- **Failure handling.** Both steps are best-effort. `SubmitFailed`, or `AllProvidersFailed`
  (which includes a meter 429 `task_budget_exceeded`), adds nothing and logs
  `pii_extra step=… failed`. `BreakerOpen` and MCP tool errors still fail the task.
- **Budget.** `BOURSE_TASK_MAX_TOKENS` stays 8192. The bench records tokens, calls and the
  budget per PII case and reports "over task budget n/N".

Upper bound with the veto dropped (replayed on the 5 pilot cases, every LLM label correct):
- dev-0005 1.000, dev-0017 1.000, dev-0023 1.000, dev-0029 1.000;
- dev-0011 0.903: tp 139, fp 30 from `sku`, recall 1.0, below 0.95;
- so 4/5 at most.

These are not measurements. The dev run `bench/configs/adr0052-b2-6a.yaml` measures both
agents on all 10 dev hard_pii cases. It reports passed n/N, F1, byte-identical n/N, s/case and
tokens/case against the budget. The test split is never read.

## Amendment 2: first dev measurement, crew fix, meter budget finding (2026-11-06)
Dev run `adr0052-ties-6a`, before the crew fix:
- Fresh stack; pii-precise 1.1.0 ACCEPTED and passed its admission exam 5/5.
- All 10 dev hard_pii cases sent straight to each agent.
- Host on AC for 28/28 samples, Low Power Mode 0/28, thermal warning 0/28.
- The run directory is kept as `data/bench/adr0052-ties-6a.crew-repeat`.

| agent | passed | F1 p50 [CI] | s/case p50 | tokens/case p50 (max) | over 8192 | LLM calls p50 |
|---|---|---|---|---|---|---|
| pii-precise 1.1.0 | 5/10 [0.24, 0.76] | 0.903 [0.873, 1.000] | 73.6 | 7688 (8529) | 2/10 | 8 |
| pii-fast 1.0.0 | 0/10 [0.00, 0.28] | 0.769 [0.607, 0.842] | 5.5 | 583 (631) | 0/10 | 1 |

Byte-identical outputs: 0/10, against 5/5 before the change.

Why the 5 pii-precise cases failed:
- dev-0011, -0053, -0059: false positives only (fp 30, 21, 24), all from the `sku` decoy. The
  union-only rule keeps those findings.
- dev-0035 and -0047: fp 17 and 20 from `sku`, plus fn 10 and 9 in `notes`.

**Finding 1: the CrewAI name step repeated itself.**
- Per-call meter rows show that 9/10 tasks repeated the same 2 calls 3 times, e.g.
  `861+243`, `1104+1`, ×3. The second call returned 1 token.
- That is 5.4-6.2k tokens per task before the extra steps.
- As a result, 4/10 free-text calls were refused with meter 429 `task_budget_exceeded`
  (`pii_extra step=free_text … failed`). Those cases lost their `notes` findings.
- Fix: the `submit` tool has `result_as_answer=True`, so its result ends the crew task with
  no separate "Final Answer" turn.
  - The unit test asserts one crew call per task. It fails with the flag off and passes
    with it on.
  - The name columns submitted are unchanged.
  - The card stays 1.1.0 (never released) and is re-examined on a fresh stack.

**Finding 2: the meter's per-task token budget is not a hard cap (ADR-0041).**
- `would_exceed` admits a call when `used + max_tokens(requested output) <= max_tokens`. It
  does not count the new call's prompt tokens, which are only known after the call.
- 2/10 tasks therefore ended above 8192 (8317 and 8529).
- Not changed here. It is a resource-limit semantics decision for the operator. The bench
  reports "over task budget n/N" either way.

## Amendment 3: dev re-measurement after the crew fix (2026-11-06)
Dev run `adr0052-ties-6a`, re-run on a fresh stack with the rebuilt image:
- pii-precise 1.1.0 ACCEPTED and passed its admission exam 5/5. The exam took about 221 s,
  against about 553 s before the fix.
- The same 10 dev hard_pii cases.
- Host: on AC 22/22 samples, Low Power Mode 0/22, thermal warning 0/22, load1 p50 6.52.
- Chain exit 0.

| agent | passed | F1 p50 [CI] | s/case p50 [CI] | tokens/case p50 [CI] (max) | over 8192 | LLM calls p50 |
|---|---|---|---|---|---|---|
| pii-precise 1.1.0 | 5/10 [0.24, 0.76] | 0.916 [0.889, 1.000] | 43.0 [31.3, 51.7] | 2903 [1887, 3216] (3225) | 0/10 [0.00, 0.28] | 3 |
| pii-fast 1.0.0 | 0/10 [0.00, 0.28] | 0.769 [0.607, 0.842] | 5.5 [4.9, 5.7] | 583 [545, 601] (631) | 0/10 [0.00, 0.28] | 1 |

Byte-identical outputs: 0/10.

Per case, pii-precise:
- fn 0 on all 10 cases (recall 1.0000), against fn 10 and 9 in `notes` on dev-0035 and
  dev-0047 before the fix.
- The 5 failures (dev-0011, -0035, -0047, -0053, -0059) are false positives only: fp 30, 17,
  20, 21, 24, all from the `sku` decoy. The union-only rule keeps those findings.
- 3 LLM calls on 8 cases and 2 on 2 cases (no FREE_TEXT column chosen). Peak task usage is
  3225 tokens, 39% of the 8192 budget.

So the per-task budget holds on dev without changing the meter. Finding 2 (it is not a hard cap)
is still open as an operator decision. With the repeat gone, no dev task comes near the cap.

## Validity threat (to be carried into RESULTS.md in Phase 7)
`contact_check` was written after the hard_pii generator, by people who knew it:
- Its email de-obfuscation forms (`[at]`, `(at)`, ` at `, `[dot]`, `(dot)`, ` dot `, case-
  insensitive, plus `{}` and `<>`) cover all three of the generator's obfuscation styles
  (`_obfuscated_email`).
- Its international-phone rule (`+`, country code, 8-15 digits) accepts all five
  `INTL_PHONES` formats.
- Its NANP-in-text rule accepts the generator's free-text form ("call <NANP> after 5pm").

Measured on 200 hard_pii cases generated with seeds `bench-dev:overlap:0..199` (dev namespace,
not in any manifest; 2026-11-06):
- `contact_check` accepts 6902/6902 planted `reach` / `tel` / `notes` contacts;
- it accepts 0/10444 non-PII cells in `notes`, `color`, `ref_no` and `batch` as PHONE or EMAIL.

On this generator the filter never blocks a correct LLM proposal and never passes a decoy. Only
the LLM's proposals, and the `sku` decoy the regex already flags, decide the score.

So pii-precise 1.1.0's recall on hard_pii measures, in part, how well the filter matches this
generator. It is not evidence about obfuscation styles the generator does not produce. The
LLM still has to propose the right columns and rows; the filter only accepts or rejects.

RESULTS.md must:
- state this threat next to every pii-precise / hard_pii number;
- report hard_pii results separately from the five standard domains;
- not present pii-precise's hard_pii recall as general robustness to obfuscated PII.

Generator and cases stay unchanged: changing them now would also be tuning, and the test
manifest pins GENERATOR_VERSION 1.

## Consequences
- The agents differ in pipeline, not just in prompt framework. The cases stay fixed.
- An LLM decides PII labels inside an agent. That is allowed: rule 10 covers trust, quarantine
  and grading, which stay deterministic. Spot checks (`check_pii`) and graders are unchanged.
- pii-precise gets slower and costlier. The bench reports that as measured.

## Status
Accepted (operator, 2026-11-06) with the corrections in the amendment. Implemented in Phase 6a
fix round 2; crew fix in amendment 2; re-measured on dev in amendment 3 (5/10 passed, 0/10 over
budget). The meter's soft cap (amendment 2, finding 2) is an open operator decision.
