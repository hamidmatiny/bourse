"""Frozen dev/test split and manifest (ADR-0011, ADR-0051).

The manifest holds seeds and sha256s, not data: cases are regenerated from their seeds and each
must hash to its manifest entry, so a generator, grader or manifest change is detected. The test
manifest's own sha256 is pinned in `<manifest>.sha256`. Loading the test split needs a
`FinalGuard`, which only `bourse.bench.final.final_preflight` constructs: tuning is dev-only.
"""

import hashlib
from pathlib import Path

from bourse.bench.generator import GENERATOR_VERSION, generate, generate_case
from bourse.contracts.bench import BenchCase, BenchManifest, Domain, ManifestEntry, Split
from bourse.grading.graders import GRADER_VERSION

MANIFEST_DIR = Path("bench/manifests")


class SplitError(Exception):
    """Manifest missing, tampered, stale, or a test-split access without a FinalGuard."""


class FinalGuard:
    """Proof that the --final preflight passed. Construct only via final_preflight()."""

    _token = object()

    def __init__(self, token: object, manifest_sha256: str) -> None:
        if token is not FinalGuard._token:
            raise SplitError("FinalGuard is issued only by final_preflight()")
        self.manifest_sha256 = manifest_sha256


def manifest_path(split: Split, root: Path = MANIFEST_DIR) -> Path:
    return root / f"{split}.json"


def pin_path(split: Split, root: Path = MANIFEST_DIR) -> Path:
    return root / f"{split}.json.sha256"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(split: Split, n: int, base_seed: str) -> BenchManifest:
    entries = []
    for i in range(n):
        case = generate_case(split, base_seed, i)
        entries.append(
            ManifestEntry(
                case_id=case.case_id, domain=case.domain, seed=case.seed, sha256=case.sha256
            )
        )
    return BenchManifest(
        split=split,
        generator_version=GENERATOR_VERSION,
        grader_version=GRADER_VERSION,
        base_seed=base_seed,
        cases=tuple(entries),
    )


def write_manifest(
    split: Split, n: int, base_seed: str, root: Path = MANIFEST_DIR, *, overwrite_dev: bool = False
) -> Path:
    """Write <split>.json and pin its sha256. The test split is written once, never again."""
    path = manifest_path(split, root)
    if path.exists() and (split == "test" or not overwrite_dev):
        raise SplitError(f"{path} exists; the {split} manifest is written once")
    manifest = build_manifest(split, n, base_seed)
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    pin_path(split, root).write_text(file_sha256(path) + "\n")
    return path


def read_manifest(split: Split, root: Path = MANIFEST_DIR) -> tuple[BenchManifest, str]:
    """Return (manifest, sha256 of the file); refuse if the pinned hash does not match."""
    path, pin = manifest_path(split, root), pin_path(split, root)
    if not path.exists() or not pin.exists():
        raise SplitError(f"missing {path} or {pin}; run `bourse bench split write --split {split}`")
    actual = file_sha256(path)
    pinned = pin.read_text().strip()
    if actual != pinned:
        raise SplitError(f"{path} sha256 {actual} != pinned {pinned} (manifest tampered)")
    manifest = BenchManifest.model_validate_json(path.read_text())
    if manifest.split != split:
        raise SplitError(f"{path} declares split {manifest.split!r}")
    return manifest, actual


def verify_cases(manifest: BenchManifest) -> list[BenchCase]:
    """Regenerate every case; refuse on any version or sha256 mismatch."""
    if manifest.generator_version != GENERATOR_VERSION:
        raise SplitError(
            f"generator version {GENERATOR_VERSION} != manifest {manifest.generator_version}"
        )
    if manifest.grader_version != GRADER_VERSION:
        raise SplitError(f"grader version {GRADER_VERSION} != manifest {manifest.grader_version}")
    cases = []
    for entry in manifest.cases:
        case = generate(manifest.split, entry.case_id, entry.domain, entry.seed)
        if case.sha256 != entry.sha256:
            raise SplitError(
                f"case {entry.case_id} sha256 {case.sha256} != manifest {entry.sha256}"
            )
        cases.append(case)
    return cases


def load_cases(
    split: Split,
    *,
    guard: FinalGuard | None = None,
    limit: int | None = None,
    domains: tuple[Domain, ...] | None = None,
    root: Path = MANIFEST_DIR,
) -> tuple[list[BenchCase], str]:
    """Verified cases of a split and the manifest sha256. The test split needs a FinalGuard."""
    if split == "test" and guard is None:
        raise SplitError("the test split is for --final runs only; use the dev split")
    manifest, sha = read_manifest(split, root)
    if guard is not None and guard.manifest_sha256 != sha:
        raise SplitError("manifest changed after the final preflight")
    cases = verify_cases(manifest)
    if domains is not None:
        cases = [c for c in cases if c.domain in domains]
    if limit is not None:
        cases = cases[:limit]
    return cases, sha
