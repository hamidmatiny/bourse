"""`bourse` command line: doctor, probe."""

import asyncio
import json
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
from bourse.registry.attacks import DEFAULT_FIXTURES, format_table, run_phase1

A2A_SPEC_VERSION = "1.0"
MCP_SPEC_VERSION = "2026-07-28"

DOCTOR_PACKAGES = ("a2a-sdk", "mcp", "langgraph", "langchain-core", "pydantic")

app = typer.Typer(no_args_is_help=True, add_completion=False)
keys_app = typer.Typer(no_args_is_help=True, help="Vendor signing keys.")
card_app = typer.Typer(no_args_is_help=True, help="Sign and verify Agent Cards.")
registry_app = typer.Typer(no_args_is_help=True, help="Agent registry service.")
attacks_app = typer.Typer(no_args_is_help=True, help="Deterministic attack suites.")
app.add_typer(keys_app, name="keys")
app.add_typer(card_app, name="card")
app.add_typer(registry_app, name="registry")
app.add_typer(attacks_app, name="attacks")


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


@keys_app.command("gen")
def keys_gen(
    vendor: Annotated[str, typer.Option(help="Vendor domain, e.g. vendor-a.local")],
    alg: Annotated[str, typer.Option(help="ES256 or EdDSA")] = "ES256",
    key_dir: Annotated[str, typer.Option(help="Private key directory (gitignored)")] = "data/keys",
) -> None:
    """Generate a signing key; write the private JWK (0600) and the public JWKS."""
    from bourse.crypto.keys import generate_vendor_keyset

    if alg not in ("ES256", "EdDSA"):
        raise typer.BadParameter("alg must be ES256 or EdDSA")
    kid, private_path, jwks_path = generate_vendor_keyset(
        vendor, Path(key_dir), "ES256" if alg == "ES256" else "EdDSA"
    )
    typer.echo(f"vendor {vendor}")
    typer.echo(f"alg {alg}")
    typer.echo(f"kid {kid}")
    typer.echo(f"private_key {private_path} (mode {private_path.stat().st_mode & 0o777:o})")
    typer.echo(f"public_jwks {jwks_path}")


@card_app.command("sign")
def card_sign(
    card_path: Annotated[Path, typer.Argument(help="Card JSON")],
    key: Annotated[Path, typer.Option(help="Private JWK file")],
    jku: Annotated[str | None, typer.Option(help="Pinned JWKS URL to put in the header")] = None,
    kid: Annotated[str | None, typer.Option(help="Key id (default: RFC 7638 thumbprint)")] = None,
    out: Annotated[Path | None, typer.Option(help="Output file (default: stdout)")] = None,
) -> None:
    """Append a detached JWS signature to a card."""
    from bourse.crypto.card_sign import sign_card
    from bourse.crypto.jcs import card_from_dict, card_to_dict
    from bourse.crypto.keys import load_key, thumbprint_kid

    private = load_key(json.loads(key.read_text()))
    card = card_from_dict(json.loads(card_path.read_text()))
    signed = sign_card(card, private, kid or thumbprint_kid(private), jku)
    text = json.dumps(card_to_dict(signed), indent=2, ensure_ascii=False) + "\n"
    if out is None:
        typer.echo(text, nl=False)
    else:
        out.write_text(text)
        typer.echo(f"written: {out}")


@card_app.command("verify")
def card_verify(
    card_path: Annotated[Path, typer.Argument(help="Signed card JSON")],
    jwks: Annotated[Path, typer.Option(help="Pinned public JWKS file")],
    jwks_url: Annotated[str, typer.Option(help="Pinned JWKS URL (compared to jku)")] = "",
) -> None:
    """Verify a card against a pinned JWKS. Exit 0 only on OK."""
    from bourse.contracts.registry import VendorKeys, VerifyResult
    from bourse.crypto.card_sign import verify_card
    from bourse.crypto.jcs import card_from_dict

    pinned = VendorKeys(jwks_url=jwks_url or "unset:", jwks=json.loads(jwks.read_text()))
    try:
        card = card_from_dict(json.loads(card_path.read_text()))
    except ValueError:
        typer.echo(VerifyResult.MALFORMED.value)
        raise typer.Exit(1) from None
    result = verify_card(card, pinned)
    typer.echo(result.value)
    raise typer.Exit(0 if result is VerifyResult.OK else 1)


@registry_app.command("serve")
def registry_serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8700,
) -> None:
    """Serve the registry API against Postgres (BOURSE_DATABASE_URL)."""
    import uvicorn
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.registry.app import create_app
    from bourse.registry.repo_sql import SqlRepository
    from bourse.registry.service import RegistryPolicy, RegistryService

    settings = get_settings()
    engine = create_async_engine(settings.bourse_database_url)
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    service = RegistryService(
        SqlRepository(engine), client, RegistryPolicy(dev_hosts=settings.dev_hosts)
    )
    app_ = create_app(service, settings.bourse_admin_token.get_secret_value())
    uvicorn.run(app_, host=host, port=port)


@attacks_app.command("phase1")
def attacks_phase1(
    fixtures: Annotated[str, typer.Option(help="Card fixture directory")] = str(DEFAULT_FIXTURES),
) -> None:
    """Run A1/A1b/A2/A3/A9 and 3 honest controls in memory; print n/N."""
    results = asyncio.run(run_phase1(Path(fixtures)))
    typer.echo(format_table(results))
    raise typer.Exit(0 if all(r.caught for r in results) else 1)


if __name__ == "__main__":
    app()
