"""The committed final configs are the Phase 6b schedule (ADR-0051 fix round 4): pin them."""

from pathlib import Path

import yaml

from bourse.adversaries.catalog import ADVERSARIES, BY_ID
from bourse.contracts.bench import PROFILE_ADVERSARIES, BenchConfig

CONFIGS = Path(__file__).resolve().parents[3] / "bench" / "configs"
DIGEST = "64ae5b1d651fb2709d5928ccb8dcfb4bb2cc9c2c8001ac34497e36a6cf905b59"


def _load(name: str) -> BenchConfig:
    return BenchConfig.model_validate(yaml.safe_load((CONFIGS / f"{name}.yaml").read_text()))


def test_every_committed_config_validates() -> None:
    files = sorted(CONFIGS.glob("*.yaml"))
    assert files
    for f in files:
        cfg = BenchConfig.model_validate(yaml.safe_load(f.read_text()))
        assert cfg.name == f.stem


def test_final_configs_cover_the_decided_set_with_decided_n() -> None:
    finals = {f.stem: _load(f.stem) for f in CONFIGS.glob("final-*.yaml")}
    assert all(c.split == "test" for c in finals.values())
    assert all(c.model_digests == {"qwen3.5:27b-q4_K_M": DIGEST} for c in finals.values())
    n = {spec.id: (spec.cases or cfg.cases) for cfg in finals.values() for spec in cfg.configs}
    assert n == {
        "B0": 50,
        "B1": 100,
        "B3": 100,
        "B3-no-spotcheck": 100,
        "B3-no-canary": 100,
        "B1-mix": 100,
        "B3-mix": 100,
    }
    assert not any(s.baseline == "B2" for c in finals.values() for s in c.configs)
    suite = {a.id for a in finals["final-suite"].adversaries}
    assert suite == {"A1", "A1b", "A2", "A3", "A9"} | {a.id for a in ADVERSARIES}
    assert all(a.mode is None for a in finals["final-suite"].adversaries)  # every variant/mode
    for profile, ids in PROFILE_ADVERSARIES.items():
        cfg = finals[f"final-{profile}"]
        assert cfg.stack_profile == profile and {a.id for a in cfg.adversaries} == set(ids)
    advs = [a for c in finals.values() for a in c.adversaries]
    assert advs and all(a.baseline_runs == 1 for a in advs)


def test_final_adversary_specs_are_capped_and_explain_non_exposure() -> None:
    """ADR-0053 amendment 1: every spec has a wall cap; registration-gated specs say why they
    get no delivery; every other runtime spec runs sole-provider with runs >= k = 3."""
    from bourse.contracts.bench import registration_gated

    finals = {f.stem: _load(f.stem) for f in CONFIGS.glob("final-*.yaml")}
    for cfg in finals.values():
        for a in cfg.adversaries:
            assert a.wall_cap_s <= 5400, (cfg.name, a.id)
            gated = registration_gated(a.id, cfg.stack_profile)
            assert bool(a.no_exposure_reason) == gated, (cfg.name, a.id)
            if a.id in BY_ID and a.id not in ("A5a", "A5b"):
                assert a.runs == 3 and a.mode is None, (cfg.name, a.id)
    caps = sum(a.wall_cap_s for a in finals["final-no-metercap"].adversaries)
    assert caps / 3600 <= 4.0  # the 3.7 h dev segment can no longer happen unbounded
