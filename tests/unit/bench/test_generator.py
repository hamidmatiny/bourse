"""Seeded generator (ADR-0051): determinism, domains, namespaces, PII truth by construction."""

import csv
import io
from typing import get_args

import pytest

from bourse.bench.generator import DOMAINS, GENERATOR_VERSION, generate, generate_case
from bourse.bench.split import read_manifest, verify_cases
from bourse.contracts.bench import Domain
from bourse.contracts.skills_io import PiiKind
from bourse.mcp_servers.pii import classify, luhn_valid, sin_valid
from bourse.trust import exam_bank


def test_same_seed_same_sha256_twice() -> None:
    for i in range(12):
        a = generate_case("dev", "seed-a", i)
        b = generate_case("dev", "seed-a", i)
        assert a.sha256 == b.sha256
        assert a.csv_text == b.csv_text and a.truth == b.truth


def test_different_seed_or_split_changes_bytes() -> None:
    a = generate_case("dev", "seed-a", 3)
    assert generate_case("dev", "seed-b", 3).sha256 != a.sha256
    assert generate_case("test", "seed-a", 3).sha256 != a.sha256


def test_domains_round_robin_and_at_least_five_plus_hard() -> None:
    assert set(DOMAINS) == set(get_args(Domain))
    assert {"people", "orders", "measurements", "clinic", "shipments", "hard_pii"} <= set(DOMAINS)
    seen = [generate_case("dev", "s", i).domain for i in range(len(DOMAINS))]
    assert seen == list(DOMAINS)


def test_seed_must_match_split_namespace() -> None:
    with pytest.raises(ValueError, match="namespace"):
        generate("dev", "x", "people", "bench-test:s:0")
    with pytest.raises(ValueError, match="namespace"):
        generate("test", "x", "people", "exam:s:0")


def _regex_pipeline(csv_text: str) -> set[tuple[int, str, str]]:
    rows = list(csv.reader(io.StringIO(csv_text)))
    found: set[tuple[int, str, str]] = set()
    for r, row in enumerate(rows[1:]):
        for col, v in zip(rows[0], row, strict=True):
            k = classify(v) if v else None
            if (
                k in ("EMAIL", "PHONE")
                or (k == "CARD" and luhn_valid(v))
                or (k == "SIN" and sin_valid(v))
            ):
                found.add((r, col, k))
    return found


def test_standard_domains_planted_pii_matches_deterministic_tools() -> None:
    """On the five standard domains the planted non-name PII is exactly what regex+Luhn+SIN
    find, so truth by construction agrees with the deterministic tools there."""
    for i in range(60):
        case = generate_case("dev", "consistency", i)
        if case.domain == "hard_pii":
            continue
        planted = {
            (f["row"], f["column"], f["kind"])
            for f in case.truth["pii"]["findings"]
            if f["kind"] != "PERSON_NAME"
        }
        assert planted == _regex_pipeline(case.csv_text), case.case_id


def test_hard_pii_contains_cases_the_regex_cannot_see_and_decoys() -> None:
    misses = 0
    decoy_flags = 0
    for i in range(5, 300, len(DOMAINS)):
        case = generate_case("dev", "hard", i)
        assert case.domain == "hard_pii"
        kinds = {f["kind"] for f in case.truth["pii"]["findings"]}
        assert kinds <= set(get_args(PiiKind))
        planted = {
            (f["row"], f["column"], f["kind"])
            for f in case.truth["pii"]["findings"]
            if f["kind"] != "PERSON_NAME"
        }
        found = _regex_pipeline(case.csv_text)
        misses += len(planted - found)
        decoy_flags += len(found - planted)
    assert misses > 0  # obfuscated emails, international phones, PII in free text
    assert decoy_flags > 0  # NANP-shaped SKUs


def test_every_case_has_numeric_stats_for_the_report_grader() -> None:
    for i in range(30):
        case = generate_case("dev", "stats", i)
        assert case.truth["stats"]["stats"], case.case_id


def test_bench_cases_disjoint_from_exam_bank() -> None:
    exam = {exam_bank.generate(f"exam:{i}").sha256 for i in range(200)}
    bench = {
        __import__("hashlib").sha256(generate_case("dev", "d", i).csv_text.encode()).hexdigest()
        for i in range(200)
    }
    assert not exam & bench


def test_committed_dev_manifest_regenerates_on_this_platform() -> None:
    """CI (Linux x86_64) regenerates every committed dev case byte-for-byte."""
    manifest, _sha = read_manifest("dev")
    assert manifest.generator_version == GENERATOR_VERSION
    assert len(verify_cases(manifest)) == len(manifest.cases)
