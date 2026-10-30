import itertools

import pytest

from bourse.registry.semver import SemVer

# Precedence chain from semver.org section 11.
CHAIN = [
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.1",
    "1.1.0",
    "1.2.0",
    "1.10.0",
    "2.0.0",
]


def test_precedence_chain() -> None:
    parsed = [SemVer(v) for v in CHAIN]
    assert parsed == sorted(parsed)
    for a, b in itertools.pairwise(parsed):
        assert a < b and not b < a and a != b


def test_build_metadata_ignored_for_precedence() -> None:
    assert SemVer("1.2.0+build.1") == SemVer("1.2.0+build.2")


@pytest.mark.parametrize(
    "raw", ["1.2", "v1.2.0", "01.2.0", "1.2.0-", "1.2.0-01", "1.2.0\n", "", "1.2.0 ", "a.b.c"]
)
def test_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        SemVer(raw)
