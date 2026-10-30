# 0012 — Agent Card signing: JCS + detached JWS, pinned JWKS only

## Context
A2A v1.0 §8.4 signs Agent Cards with a JWS (RFC 7515) over the RFC 8785 (JCS) canonical form of
the card. Attacks A1 (forgery), A2 (tampering) and A3 (jku swap) must be rejected
deterministically.

## SDK findings (a2a-sdk 1.2.3, inspected in `.venv`)
- `a2a.utils.signing` provides `create_agent_card_signer` and `create_signature_verifier`, built on
  PyJWT (`a2a-sdk[signing]` extra). The verifier takes a caller-supplied
  `key_provider(kid, jku)` and an algorithm list; it does not decide whether a jku is trusted.
- `a2a.utils._jcs` is the SDK's own RFC 8785 implementation (max depth 128).
- Payload rule (`_canonicalize_agent_card`): `MessageToDict(card)` → drop `signatures` →
  recursively drop empty strings, lists and objects → JCS.
- Signatures are stored as `AgentCardSignature{protected, signature, header}`: the compact JWS
  with the payload segment removed (detached payload).

## Decision
- Our implementation: `bourse.crypto` (jwcrypto 1.6.1 for JWS, rfc8785 0.1.4 for JCS). It uses the
  SDK's payload rule exactly (`bourse.crypto.jcs.signing_payload`), so signatures interoperate.
  Unit tests check: payload bytes equal the SDK's for every fixture; SDK-signed cards verify
  with ours; our signatures verify with the SDK verifier; PyJWT verifies ours and PyJWT-made
  signatures verify with ours.
- Keys: ES256 (P-256) default, EdDSA (Ed25519) optional; `kid` = RFC 7638 SHA-256 thumbprint.
  Private keys only under `data/keys/` (gitignored, mode 0600). Pinned JWKS reject private
  members (`d`, `p`, `q`, `dp`, `dq`, `qi`, `oth`, `k`).
- Protected header: `{alg, kid, typ: "JOSE", jku?}`.
- Verification policy (`verify_card` → `VerifyResult`):
  - alg allowlist {ES256, EdDSA}; anything else → UNSUPPORTED_ALG (before key lookup).
  - Keys come only from the vendor's pinned JWKS. `jku` is never fetched; if present and not equal
    to the pinned `jwks_url` → UNTRUSTED_JKU. Embedded keys (`jwk`, `x5u`, `x5c`) → UNTRUSTED_JKU.
  - `crit`, `b64`, or key-selecting members in the unprotected header → MALFORMED.
  - kid not in pinned JWKS → UNKNOWN_KID; key type not matching alg, or bad signature →
    BAD_SIGNATURE. At least one valid signature → OK. With several failures the reported result
    is the first of UNTRUSTED_JKU, UNSUPPORTED_ALG, BAD_SIGNATURE, UNKNOWN_KID, MALFORMED.
- Mapping to registry reasons: UNKNOWN_KID → FORGED_SIGNATURE; BAD_SIGNATURE → TAMPERED.
  A forgery that reuses a pinned kid with the attacker's key is indistinguishable from tampering
  without the original payload, so it is reported as TAMPERED (unit test
  `test_attacker_key_with_spoofed_kid_is_bad_signature`).

## Consequences
- The SDK's signing helpers are used only in tests (interop), so PyJWT stays a dev dependency.
- Changing the payload rule breaks every stored signature; it would need a new ADR.

## Status
Accepted (Phase 1).
