"""Configured models must match the ADR-0047 honest-agent roster (single source)."""

from bourse.agents.catalog import (
    ROSTER_LARGE_MODEL,
    ROSTER_MODEL,
    ROSTER_MODELS,
    ROSTER_SMALL_MODEL,
    specs_from_settings,
)
from bourse.config import Settings, get_settings
from bourse.registry.service import make_agent_id


def test_settings_defaults_match_adr_0047_roster() -> None:
    s = Settings()
    assert ROSTER_SMALL_MODEL == ROSTER_LARGE_MODEL == ROSTER_MODEL == "qwen3.5:27b-q4_K_M"
    assert s.bourse_model_small == ROSTER_MODEL
    assert s.bourse_model_large == ROSTER_MODEL
    assert s.roster_models == frozenset({ROSTER_MODEL})


def test_agent_specs_models_match_roster() -> None:
    specs = specs_from_settings(Settings())
    assert set(specs) == set(ROSTER_MODELS)
    for name, model in ROSTER_MODELS.items():
        assert specs[name].model == model, f"{name}: {specs[name].model} != {model}"
        assert model == ROSTER_MODEL


def test_broker_agent_models_match_roster() -> None:
    s = Settings()
    specs = specs_from_settings(s)
    expected = {
        make_agent_id(spec.display_name, spec.vendor_domain): ROSTER_MODELS[name]
        for name, spec in specs.items()
    }
    assert s.bourse_broker_agent_models == expected


def test_get_settings_roster_or_env_override_documented() -> None:
    """Loaded settings must still resolve specs to the roster unless env overrides models.

    This catches .env / compose env drift from ADR-0047 when no override is intended.
    """
    s = get_settings()
    assert s.bourse_model_small == ROSTER_MODEL, (
        f"BOURSE_MODEL_SMALL={s.bourse_model_small!r} differs from ADR-0047 "
        f"{ROSTER_MODEL!r}; revert or file an ADR with memory+exam numbers"
    )
    assert s.bourse_model_large == ROSTER_MODEL
    for name, spec in specs_from_settings(s).items():
        assert spec.model == ROSTER_MODELS[name]
