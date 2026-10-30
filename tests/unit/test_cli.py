from pathlib import Path

from typer.testing import CliRunner

from bourse.cli import app


def test_doctor_prints_versions() -> None:
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for needle in (
        "python 3.11",
        "a2a-sdk 1.2.3",
        "mcp 2.",
        "langgraph ",
        "langchain-core ",
        "pydantic 2.",
        "A2A_SPEC_VERSION 1.0",
        "MCP_SPEC_VERSION 2026-07-28",
    ):
        assert needle in result.output
    assert "NOT INSTALLED" not in result.output


def test_attacks_phase1_cli() -> None:
    result = CliRunner().invoke(app, ["attacks", "phase1"])
    assert result.exit_code == 0, result.output
    assert "attacks caught: 8/8" in result.output
    assert "honest false rejects: 0/3" in result.output


def test_keys_gen_sign_verify_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app, ["keys", "gen", "--vendor", "vendor-a.local", "--key-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "(mode 600)" in result.output
    key_file = next((tmp_path / "vendor-a.local").glob("*.private.jwk.json"))
    jwks_file = tmp_path / "vendor-a.local" / "jwks.json"
    signed = tmp_path / "signed.json"
    url = "https://vendor-a.local/.well-known/jwks.json"
    card = "tests/fixtures/cards/honest-a-profiler.json"
    result = runner.invoke(
        app, ["card", "sign", card, "--key", str(key_file), "--jku", url, "--out", str(signed)]
    )
    assert result.exit_code == 0, result.output
    ok = runner.invoke(
        app, ["card", "verify", str(signed), "--jwks", str(jwks_file), "--jwks-url", url]
    )
    assert ok.exit_code == 0 and ok.output.strip() == "OK"
    bad = runner.invoke(app, ["card", "verify", str(signed), "--jwks", str(jwks_file)])
    assert bad.exit_code == 1 and bad.output.strip() == "UNTRUSTED_JKU"
