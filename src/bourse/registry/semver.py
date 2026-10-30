"""Semantic Versioning 2.0.0 parsing and precedence (build metadata ignored for precedence)."""

import re
from functools import total_ordering

_IDENT = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({_IDENT}(?:\.{_IDENT})*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


@total_ordering
class SemVer:
    __slots__ = ("major", "minor", "patch", "prerelease", "raw")

    def __init__(self, raw: str) -> None:
        match = _SEMVER.fullmatch(raw)
        if match is None:
            raise ValueError(f"not a semantic version: {raw!r}")
        self.raw = raw
        self.major, self.minor, self.patch = (int(match.group(i)) for i in (1, 2, 3))
        pre = match.group(4)
        self.prerelease: tuple[str, ...] = tuple(pre.split(".")) if pre else ()

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    @staticmethod
    def _pre_lt(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
        if not a:
            return False  # release > any prerelease
        if not b:
            return True
        for x, y in zip(a, b, strict=False):
            if x == y:
                continue
            if x.isdigit() and y.isdigit():
                return int(x) < int(y)
            if x.isdigit() != y.isdigit():
                return x.isdigit()  # numeric identifiers sort before alphanumeric
            return x < y
        return len(a) < len(b)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() == other._key() and self.prerelease == other.prerelease

    def __lt__(self, other: "SemVer") -> bool:
        if self._key() != other._key():
            return self._key() < other._key()
        return self._pre_lt(self.prerelease, other.prerelease)

    def __hash__(self) -> int:
        return hash((self._key(), self.prerelease))

    def __repr__(self) -> str:
        return f"SemVer({self.raw!r})"
