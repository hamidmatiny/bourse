import pytest

from bourse.contracts.skills import SkillId
from bourse.grading.graders import grade
from bourse.trust.exam_bank import BENCH_NAMESPACES, EXAM_NS, NAMESPACES, generate
from bourse.trust.honeytokens import derive


def test_namespaces_disjoint_by_prefix_and_dataset_sha256() -> None:
    assert EXAM_NS not in BENCH_NAMESPACES
    for a in NAMESPACES:
        for b in NAMESPACES:
            if a != b:
                assert not a.startswith(b)
    shas = {ns: {generate(f"{ns}{i}").sha256 for i in range(200)} for ns in NAMESPACES}
    assert all(len(s) == 200 for s in shas.values())
    assert not (shas["exam:"] & shas["bench-dev:"])
    assert not (shas["exam:"] & shas["bench-test:"])
    assert not (shas["bench-dev:"] & shas["bench-test:"])


def test_seed_outside_namespaces_rejected() -> None:
    with pytest.raises(ValueError):
        generate("misc:1")


def test_seeded_and_truth_grades_itself() -> None:
    a, b = generate("exam:agent:pii.detect:1"), generate("exam:agent:pii.detect:1")
    assert a.csv_text == b.csv_text
    keys = {
        SkillId.SCHEMA_PROFILE: "profile",
        SkillId.STATS_COMPUTE: "stats",
        SkillId.PII_DETECT: "pii",
        SkillId.UNITS_NORMALIZE: "units",
    }
    for skill, key in keys.items():
        assert grade(skill, a.truth[key], a.truth).passed
    stats = [s["column"] for s in a.truth["stats"]["stats"]]
    assert stats == ["id", "age", "weight_lb", "dist_km"]
    assert len(a.truth["units"]["conversions"]) == 2


def test_honeytoken_in_dataset_and_truth() -> None:
    token = derive(b"secret", "run-1:pii:1")
    ds = generate("exam:x:1", token)
    assert token.email in ds.csv_text
    findings = {(f["row"], f["column"], f["kind"]) for f in ds.truth["pii"]["findings"]}
    assert (0, "card", "CARD") in findings and (0, "email", "EMAIL") in findings
