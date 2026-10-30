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
