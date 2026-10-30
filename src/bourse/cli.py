"""`bourse` command line: doctor, probe."""

import asyncio
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import httpx
import typer

from bourse import __version__
from bourse.config import get_settings
from bourse.llm.ollama import DEFAULT_HTTP_TIMEOUT
from bourse.llm.probe import run_probe

A2A_SPEC_VERSION = "1.0"
MCP_SPEC_VERSION = "2026-07-28"

DOCTOR_PACKAGES = ("a2a-sdk", "mcp", "langgraph", "langchain-core", "pydantic")

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "NOT INSTALLED"


@app.command()
def doctor() -> None:
    """Print interpreter, pinned library versions and spec versions."""
    typer.echo(f"bourse {__version__}")
    typer.echo(f"python {platform.python_version()}")
    for name in DOCTOR_PACKAGES:
        typer.echo(f"{name} {_pkg_version(name)}")
    typer.echo(f"A2A_SPEC_VERSION {A2A_SPEC_VERSION}")
    typer.echo(f"MCP_SPEC_VERSION {MCP_SPEC_VERSION}")


async def _probe(base_url: str, model: str, out_dir: Path) -> int:
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        report, written = await run_probe(client, base_url, model, out_dir)
    typer.echo(report.model_dump_json(indent=2))
    typer.echo(f"written: {written}")
    failures: list[str] = []
    if not report.tool_call_ok:
        failures.append("tool_call_ok=false")
    if not report.think_false_ok:
        failures.append("think_false_ok=false")
    if not report.resident_ok:
        failures.append("resident_ok=false")
    if failures:
        typer.echo(f"PROBE FAIL: {', '.join(failures)}", err=True)
        return 1
    typer.echo("PROBE OK")
    return 0


@app.command()
def probe(
    model: Annotated[str | None, typer.Option(help="Model to probe (default: small)")] = None,
    out_dir: Annotated[str, typer.Option(help="Directory for probe JSON")] = "data/probe",
) -> None:
    """Probe the local Ollama server and write data/probe/probe-<utc>.json."""
    settings = get_settings()
    chosen = model or settings.bourse_model_small
    code = asyncio.run(_probe(settings.ollama_base_url, chosen, Path(out_dir)))
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
