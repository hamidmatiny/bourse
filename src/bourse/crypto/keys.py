"""Signing keys: ES256 (P-256) and optional EdDSA (Ed25519); kid is the RFC 7638 thumbprint.

Private keys are written only under data/keys/ (gitignored) with mode 0600.
"""

import json
import os
from pathlib import Path
from typing import Any, Literal

from jwcrypto import jwk

Alg = Literal["ES256", "EdDSA"]

DEFAULT_KEY_DIR = Path("data/keys")


def generate_key(alg: Alg = "ES256") -> jwk.JWK:
    if alg == "ES256":
        return jwk.JWK.generate(kty="EC", crv="P-256")
    return jwk.JWK.generate(kty="OKP", crv="Ed25519")


def key_alg(key: jwk.JWK) -> Alg:
    params: dict[str, Any] = key.export_public(as_dict=True)
    if params.get("kty") == "EC" and params.get("crv") == "P-256":
        return "ES256"
    if params.get("kty") == "OKP" and params.get("crv") == "Ed25519":
        return "EdDSA"
    raise ValueError(f"unsupported key type {params.get('kty')}/{params.get('crv')}")


def thumbprint_kid(key: jwk.JWK) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url)."""
    return str(key.thumbprint())


def public_jwk(key: jwk.JWK) -> dict[str, Any]:
    params: dict[str, Any] = key.export_public(as_dict=True)
    params.update({"kid": thumbprint_kid(key), "alg": key_alg(key), "use": "sig"})
    return params


def private_jwk(key: jwk.JWK) -> dict[str, Any]:
    params: dict[str, Any] = key.export_private(as_dict=True)
    params.update({"kid": thumbprint_kid(key), "alg": key_alg(key), "use": "sig"})
    return params


def jwks(keys: list[jwk.JWK]) -> dict[str, Any]:
    return {"keys": [public_jwk(k) for k in keys]}


def load_key(data: dict[str, Any]) -> jwk.JWK:
    return jwk.JWK(**data)


def write_private_key(key: jwk.JWK, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(private_jwk(key), fh, indent=2)
        fh.write("\n")


def generate_vendor_keyset(
    vendor: str, key_dir: Path = DEFAULT_KEY_DIR, alg: Alg = "ES256"
) -> tuple[str, Path, Path]:
    """Create a key for `vendor`; returns (kid, private key path, public JWKS path)."""
    key = generate_key(alg)
    kid = thumbprint_kid(key)
    vendor_dir = key_dir / vendor
    private_path = vendor_dir / f"{kid}.private.jwk.json"
    write_private_key(key, private_path)
    jwks_path = vendor_dir / "jwks.json"
    existing: list[dict[str, Any]] = []
    if jwks_path.exists():
        existing = list(json.loads(jwks_path.read_text()).get("keys", []))
    existing.append(public_jwk(key))
    jwks_path.write_text(json.dumps({"keys": existing}, indent=2) + "\n")
    return kid, private_path, jwks_path
