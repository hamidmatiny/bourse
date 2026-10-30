# Dataset regression fixtures (Phase 2a)

Three hand-built CSVs with ground truth in `<name>.truth.json`. Truth was computed independently
of `bourse` code: PII labels come from how each cell was constructed (cards and SINs built with
a valid or deliberately wrong Luhn check digit), statistics from Python's `statistics` module
(`fmean`, sample `stdev`, `median`), unit checks from explicit formulas such as
`(F - 32) * 5 / 9`. Phase 6 adds a seeded generator; these stay as regression fixtures.

| File | Rows | Contents |
|---|---|---|
| people.csv | 30 | names, emails, phones (4 formats), cards (Visa/MC/Amex/Discover, valid and invalid Luhn, 4111 1111 1111 1111 / ...1112), SINs (valid, bad checksum, `046 454 286` = Luhn-valid but first digit 0), dates, bools, ints with nulls |
| measurements.csv | 25 | `weight_lb`, `temp_F` (rows 3/4 are 32.0 and 212.0), `dist_km` (one null), `dur_min`, `site` |
| orders.csv | 20 | customers, amounts, dates, contact (alternating email/phone), notes, bools, `vol_L` |

Truth keys: `profile` (SchemaProfileOut), `stats` (StatsOut, numeric columns in header order),
`pii` (PiiOut: EMAIL/PHONE always, CARD only when Luhn-valid, SIN only when valid,
PERSON_NAME for name columns), `regex_candidates` (per column, 0-based row index and kind,
invalid cards/SINs included), `luhn_cases`, `sin_cases`, `units` (UnitsOut), `conversion_checks`.
