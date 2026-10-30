"""Shared helpers for registry tests: in-memory keys, signed fixture cards, services."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from jwcrypto import jwk

from bourse.contracts.registry import VendorCreate, VendorKeys
from bourse.crypto.card_sign import sign_card
from bourse.crypto.jcs import card_from_dict, card_to_dict
from bourse.crypto.keys import Alg, generate_key, jwks, thumbprint_kid
from bourse.registry.repo import Repository
from bourse.registry.repo_memory import InMemoryRepository
from bourse.registry.service import RegistryPolicy, RegistryService

FIXTURES = Path(__file__).parent / "fixtures" / "cards"
FIXTURE_NAMES = sorted(p.name for p in FIXTURES.glob("*.json"))


def load_card(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@dataclass(frozen=True)
class Party:
    domain: str
    key: jwk.JWK

    @property
    def kid(self) -> str:
        return thumbprint_kid(self.key)

    @property
    def jwks_url(self) -> str:
        return f"https://{self.domain}/.well-known/jwks.json"

    @property
    def keys(self) -> VendorKeys:
        return VendorKeys(jwks_url=self.jwks_url, jwks=jwks([self.key]))

    def sign(
        self, card: dict[str, Any], *, kid: str | None = None, jku: str | None = None
    ) -> dict[str, Any]:
        signed = sign_card(card_from_dict(card), self.key, kid or self.kid, jku)
        return card_to_dict(signed)


def make_party(domain: str, alg: Alg = "ES256") -> Party:
    return Party(domain, generate_key(alg))


async def make_service(
    *parties: Party,
    repo: Repository | None = None,
    client: httpx.AsyncClient | None = None,
    policy: RegistryPolicy | None = None,
) -> RegistryService:
    service = RegistryService(repo or InMemoryRepository(), client, policy)
    for party in parties:
        await service.add_vendor(
            VendorCreate(domain=party.domain, name=party.domain, keys=party.keys)
        )
    return service
