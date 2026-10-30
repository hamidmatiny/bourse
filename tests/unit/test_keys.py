import base64
import hashlib
import json
import stat
from pathlib import Path

from bourse.contracts.registry import PRIVATE_JWK_MEMBERS
from bourse.crypto.keys import (
    generate_key,
    generate_vendor_keyset,
    jwks,
    private_jwk,
    public_jwk,
    thumbprint_kid,
)


def _rfc7638(members: dict[str, str]) -> str:
    canonical = json.dumps(members, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()


def test_es256_kid_is_rfc7638_thumbprint() -> None:
    key = generate_key("ES256")
    pub = public_jwk(key)
    expected = _rfc7638({m: pub[m] for m in ("crv", "kty", "x", "y")})
    assert thumbprint_kid(key) == expected == pub["kid"]
    assert pub["alg"] == "ES256"


def test_eddsa_kid_is_rfc7638_thumbprint() -> None:
    key = generate_key("EdDSA")
    pub = public_jwk(key)
    assert thumbprint_kid(key) == _rfc7638({m: pub[m] for m in ("crv", "kty", "x")})
    assert pub["alg"] == "EdDSA"


def test_public_exports_have_no_private_members() -> None:
    key = generate_key()
    assert not PRIVATE_JWK_MEMBERS & public_jwk(key).keys()
    assert all(not PRIVATE_JWK_MEMBERS & k.keys() for k in jwks([key])["keys"])
    assert "d" in private_jwk(key)


def test_generate_vendor_keyset_writes_0600_private_key(tmp_path: Path) -> None:
    kid, private_path, jwks_path = generate_vendor_keyset("vendor-a.local", tmp_path)
    assert private_path.parent == tmp_path / "vendor-a.local"
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    published = json.loads(jwks_path.read_text())
    assert [k["kid"] for k in published["keys"]] == [kid]
    assert "d" not in published["keys"][0]
    kid2, _, _ = generate_vendor_keyset("vendor-a.local", tmp_path)
    assert [k["kid"] for k in json.loads(jwks_path.read_text())["keys"]] == [kid, kid2]
