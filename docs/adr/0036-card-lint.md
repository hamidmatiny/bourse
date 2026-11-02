# 0036 — Card lint

## Context
Card text never reaches an LLM (ADR-0008), but cards are shown to operators and copied into
reports, and text aimed at a model is a signal of intent.

## Decision
`registry/lint.py` runs at registration and refresh, after signature and domain checks, over
name, description, provider.organization, skill id/name/description/tags/examples,
documentationUrl and iconUrl.
- HARD (rejected, HARD violation; an ACTIVE agent refreshed with such a card is quarantined
  by the existing ADR-0013 rule):
  - `LINT_INJECTION`: configurable markers (default: "ignore (all) previous", "system
    prompt", "you must", "disregard", chat-template tokens `<|...|>`, function_call /
    tool_call(s) / tool_use, JSON `{"name":` / `{"arguments":`, ReAct line prefixes) and
    base64-like runs longer than 64 characters.
  - `HIDDEN_UNICODE`: zero-width (U+200B-U+200F, U+2060-U+2064, U+FEFF), bidi controls
    (U+202A-U+202E, U+2066-U+2069), tag characters (U+E0000-U+E007F).
  - `MARKUP`: HTML/XML tags.
  - `FOREIGN_URL`: a URL whose host is not the vendor domain or a subdomain.
- SOFT (accepted, `LINT_WARNING` violation): description > 500 chars, skill description > 300,
  more than 5 examples, an example > 200 chars.

## Consequences
- The 5 agent cards and the 6 Phase 1 fixture cards pass lint (unit-tested for the 5).
- Lint is lexical; paraphrased instructions pass it. It is one layer, not the defense.

## Status
Accepted (Phase 5a).
