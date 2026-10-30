# Phase 1 card fixtures

Unsigned A2A v1.0 base cards. Signatures are added at run time by
`bourse.registry.attacks.run_phase1` with keys generated in memory; no private key is stored in
the repo.

| File | Used by | Expected |
|---|---|---|
| honest-a-profiler.json | H1 control (vendor-a.local); A2 x3 and A3 start from its signed form | accepted |
| honest-a-stats.json | H2 control (vendor-a.local) | accepted |
| honest-b-pii.json | H3 control (vendor-b.local; non-ASCII description) | accepted |
| a1-forged-a-profiler.json | A1: claims vendor-a.local, signed with the attacker key | FORGED_SIGNATURE |
| a1b-impersonate-a.json | A1b: signed by attacker.local, interface URL on vendor-a.local | DOMAIN_MISMATCH |
| a9-a-units.json | A9: registered at 1.2.0, then 1.1.0 (rollback) and a modified 1.2.0 (reuse) | VERSION_ROLLBACK, VERSION_REUSE |
