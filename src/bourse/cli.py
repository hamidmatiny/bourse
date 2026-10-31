"""`bourse` command line: doctor, probe."""

import asyncio
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, cast

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
mcp_app = typer.Typer(no_args_is_help=True, help="Pure MCP tool servers.")
meter_app = typer.Typer(no_args_is_help=True, help="Metering LLM proxy.")
agent_app = typer.Typer(no_args_is_help=True, help="A2A agents.")
stack_app = typer.Typer(no_args_is_help=True, help="Local stack of all services.")
smoke_app = typer.Typer(no_args_is_help=True, help="Smoke runs against the local stack.")
runs_app = typer.Typer(no_args_is_help=True, help="Broker run records.")
e2e_app = typer.Typer(no_args_is_help=True, help="Broker end-to-end runs on fixtures.")
usage_app = typer.Typer(no_args_is_help=True, help="Metered LLM usage.")
ledger_app = typer.Typer(no_args_is_help=True, help="Delegation ledger.")
app.add_typer(keys_app, name="keys")
app.add_typer(card_app, name="card")
app.add_typer(registry_app, name="registry")
app.add_typer(attacks_app, name="attacks")
app.add_typer(mcp_app, name="mcp")
app.add_typer(meter_app, name="meter")
app.add_typer(agent_app, name="agent")
app.add_typer(stack_app, name="stack")
app.add_typer(smoke_app, name="smoke")
app.add_typer(runs_app, name="runs")
app.add_typer(e2e_app, name="e2e")
app.add_typer(usage_app, name="usage")
app.add_typer(ledger_app, name="ledger")


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


async def _probe(base_url: str, model: str, out_dir: Path, num_ctx: int) -> int:
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        report, written = await run_probe(client, base_url, model, out_dir, num_ctx=num_ctx)
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
    code = asyncio.run(
        _probe(settings.ollama_base_url, chosen, Path(out_dir), settings.bourse_meter_num_ctx)
    )
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
    port: Annotated[int, typer.Option()] = 55300,
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


@mcp_app.command("serve")
def mcp_serve(
    server: Annotated[str, typer.Argument(help="table | pii | units")],
    port: Annotated[
        int | None, typer.Option(help="Default: table 55101, pii 55102, units 55103")
    ] = None,
) -> None:
    """Serve one tool server over streamable HTTP (stateless, 127.0.0.1 only)."""
    from bourse.mcp_servers.registry import ServerName, serve

    if server not in ("table", "pii", "units"):
        raise typer.BadParameter("server must be table, pii or units")
    serve(cast(ServerName, server), port)


@meter_app.command("serve")
def meter_serve(
    port: Annotated[int, typer.Option()] = 55200,
) -> None:
    """Serve the metering proxy on 127.0.0.1 (usage rows to Postgres)."""
    import uvicorn
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.llm.base import Provider, Sampling
    from bourse.llm.ollama import OllamaProvider
    from bourse.llm.omniroute import OmniRouteProvider
    from bourse.meter.app import MeterConfig, create_meter_app
    from bourse.meter.keys import parse_keys
    from bourse.meter.repo_sql import SqlMeterRepository

    settings = get_settings()
    keys = parse_keys(settings.bourse_meter_keys.get_secret_value())
    if not keys:
        raise typer.BadParameter("BOURSE_METER_KEYS is empty; set agent_id:key pairs in .env")
    from bourse.telemetry import setup_tracing, traced_async_client

    setup_tracing("bourse-meter", settings.otel_exporter_otlp_endpoint)
    client = traced_async_client(timeout=DEFAULT_HTTP_TIMEOUT)
    providers: dict[str, list[Provider]] = {}
    for model in (settings.bourse_model_large, settings.bourse_model_small):
        chain: list[Provider] = [OllamaProvider(settings.ollama_base_url, model, client)]
        if settings.omniroute_model:
            chain.append(
                OmniRouteProvider(
                    settings.omniroute_base_url,
                    settings.omniroute_model,
                    settings.omniroute_api_key.get_secret_value(),
                    client,
                )
            )
        providers[model] = chain
    app_ = create_meter_app(
        providers=providers,
        keys=keys,
        repo=SqlMeterRepository(create_async_engine(settings.bourse_database_url)),
        config=MeterConfig(
            max_tokens_per_call=settings.bourse_meter_max_tokens_per_call,
            deadline_s=settings.bourse_meter_deadline_s,
            sampling=Sampling(
                temperature=settings.bourse_meter_temperature,
                seed=settings.bourse_meter_seed,
                num_ctx=settings.bourse_meter_num_ctx,
                keep_alive=settings.bourse_meter_keep_alive,
            ),
        ),
    )
    uvicorn.run(app_, host="127.0.0.1", port=port)


@agent_app.command("serve")
def agent_serve(name: Annotated[str, typer.Argument(help="Agent name")]) -> None:
    """Serve one agent (A2A JSON-RPC + signed card) on its port, 127.0.0.1 only."""
    from bourse.agents.catalog import AGENT_NAMES, serve_agent

    if name not in AGENT_NAMES:
        raise typer.BadParameter(f"agent must be one of {', '.join(AGENT_NAMES)}")
    settings = get_settings()
    key = settings.bourse_agent_key.get_secret_value()
    if not key:
        raise typer.BadParameter("BOURSE_AGENT_KEY is not set (bourse stack up sets it)")
    serve_agent(name, settings, settings.bourse_meter_url, key)


@stack_app.command("up")
def stack_up() -> None:
    """Start MCP servers, meter, registry and agents; wait for health; print a table."""
    from bourse.stack.launcher import services, start, table, wait_healthy

    svcs = services(get_settings())
    pids = {s.name: start(s) for s in svcs}
    health = wait_healthy(svcs)
    rows = [["name", "port", "pid", "healthy"]]
    rows += [[s.name, str(s.port), str(pids[s.name]), str(health[s.name])] for s in svcs]
    typer.echo(table(rows))
    up = sum(health.values())
    typer.echo(f"healthy {up}/{len(svcs)} (logs: data/logs/<name>.log)")
    raise typer.Exit(0 if up == len(svcs) else 1)


@stack_app.command("down")
def stack_down() -> None:
    """Stop every service started by `bourse stack up`."""
    from bourse.stack.launcher import services, stop

    svcs = services(get_settings())
    stopped = sum(stop(s.name) for s in svcs)
    typer.echo(f"stopped {stopped}/{len(svcs)}")


@stack_app.command("status")
def stack_status() -> None:
    """Show pid and health of every service."""
    from bourse.stack.launcher import healthy, read_pid, services, table

    svcs = services(get_settings())
    rows = [["name", "port", "pid", "healthy"]]
    ok = 0
    for s in svcs:
        pid = read_pid(s.name)
        h = pid is not None and healthy(s)
        ok += h
        rows.append([s.name, str(s.port), str(pid or "-"), str(h)])
    typer.echo(table(rows))
    typer.echo(f"healthy {ok}/{len(svcs)}")
    raise typer.Exit(0 if ok == len(svcs) else 1)


@stack_app.command("register")
def stack_register() -> None:
    """Onboard vendor-a/b/c with pinned JWKS and register the 5 agents by card_url."""
    from bourse.stack.launcher import table
    from bourse.stack.register import register_all

    rows, accepted, total = register_all(get_settings())
    typer.echo(table(rows))
    typer.echo(f"registered {accepted}/{total} ACCEPTED")
    raise typer.Exit(0 if accepted == total else 1)


@stack_app.command("activate")
def stack_activate(
    all_agents: Annotated[bool, typer.Option("--all", help="Activate every PENDING agent")] = False,
) -> None:
    """Admin PENDING -> ACTIVE before the admission exam exists (ADR-0028)."""
    from bourse.stack.launcher import table
    from bourse.stack.register import activate_all

    if not all_agents:
        typer.echo("pass --all (per-agent activation is not offered)", err=True)
        raise typer.Exit(2)
    rows, activated, total = activate_all(get_settings())
    typer.echo(table(rows))
    typer.echo(f"activated {activated}/{total}")
    raise typer.Exit(0 if activated == total else 1)


@smoke_app.command("all")
def smoke_all_cmd(
    datasets: Annotated[str, typer.Option(help="Comma-separated dataset names")] = (
        "people,measurements,orders"
    ),
    agents: Annotated[str | None, typer.Option(help="Comma-separated agent names")] = None,
) -> None:
    """Every agent x skill x dataset through A2A; grade; read metered usage by task_id."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.meter.repo_sql import SqlMeterRepository
    from bourse.stack.launcher import table
    from bourse.stack.smoke import result_table, smoke_all, summary_lines

    settings = get_settings()

    async def run() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            rows, out = await smoke_all(
                settings,
                SqlMeterRepository(engine),
                [d.strip() for d in datasets.split(",") if d.strip()],
                echo=lambda line: typer.echo(line),
                agents=[a.strip() for a in agents.split(",")] if agents else None,
            )
        finally:
            await engine.dispose()
        typer.echo(table(result_table(rows)))
        for line in summary_lines(rows):
            typer.echo(line)
        typer.echo(f"written: {out}")
        return 0 if all(r.state == "COMPLETED" for r in rows) else 1

    raise typer.Exit(asyncio.run(run()))


@app.command("run")
def run_cmd(
    dataset: Annotated[str, typer.Option(help="Fixture dataset name")],
    request: Annotated[str, typer.Option(help="Request text (<= 1000 chars)")],
) -> None:
    """One broker run: plan, select, delegate over A2A; print a graded subtask table."""
    from bourse.broker.runner import (
        broker_from_settings,
        intake,
        load_fixture,
        subtask_rows,
        subtask_table,
    )
    from bourse.stack.launcher import table

    settings = get_settings()

    async def run() -> int:
        csv_text, truth = load_fixture(dataset)
        async with broker_from_settings(settings, echo=lambda line: typer.echo(line)) as session:
            typer.echo(f"dataset {dataset}")
            report = await session.broker.start(intake(settings, csv_text, request))
            rows = await subtask_rows(session.broker, report, truth)
            resident = session.monitor.summary()
        typer.echo(table(subtask_table(rows)))
        typer.echo(f"run {report.run_id} {report.status.value} planner={report.planner_used}")
        if report.abort_reason:
            typer.echo(f"abort_reason {report.abort_reason}")
        typer.echo(f"graded passed {sum(r.passed for r in rows)}/{len(rows)}")
        typer.echo(f"tokens in {report.tokens_in} out {report.tokens_out}")
        typer.echo(f"wall_s {report.wall_ms / 1000:.1f}")
        typer.echo(resident)
        typer.echo(f"trace {report.trace_id}")
        return 0 if report.status.value == "SUCCEEDED" else 1

    raise typer.Exit(asyncio.run(run()))


@runs_app.command("show")
def runs_show(run_id: Annotated[str, typer.Argument(help="Run id")]) -> None:
    """Print a stored run and its delegation rows."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.broker.repo_sql import SqlRunRepository
    from bourse.stack.launcher import table

    settings = get_settings()

    async def show() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            repo = SqlRunRepository(engine)
            run = await repo.get_run(run_id)
            if run is None:
                typer.echo(f"unknown run {run_id}", err=True)
                return 1
            delegations = await repo.list_delegations(run_id)
        finally:
            await engine.dispose()
        typer.echo(
            json.dumps(run.model_dump(mode="json", exclude={"plan"}), indent=2, sort_keys=True)
        )
        rows = [["subtask", "skill", "agent", "attempt", "state", "error_kind", "wall_ms"]]
        for d in delegations:
            rows.append(
                [
                    d.subtask_id,
                    d.skill.value,
                    d.agent_id,
                    str(d.attempt),
                    d.state.value,
                    d.error_kind or "-",
                    f"{d.wall_ms:.0f}",
                ]
            )
        typer.echo(table(rows))
        return 0

    raise typer.Exit(asyncio.run(show()))


@runs_app.command("resume")
def runs_resume(run_id: Annotated[str, typer.Argument(help="Run id")]) -> None:
    """Continue a killed run from its last checkpoint; completed levels are not re-delegated."""
    from bourse.broker.runner import broker_from_settings

    settings = get_settings()

    async def resume() -> int:
        async with broker_from_settings(settings, echo=lambda line: typer.echo(line)) as session:
            report = await session.broker.resume(run_id)
            resident = session.monitor.summary()
        typer.echo(f"run {report.run_id} {report.status.value}")
        typer.echo(resident)
        return 0 if report.status.value == "SUCCEEDED" else 1

    raise typer.Exit(asyncio.run(resume()))


@e2e_app.command("all")
def e2e_all_cmd(
    datasets: Annotated[str, typer.Option(help="Comma-separated dataset names")] = (
        "people,measurements,orders"
    ),
    repeat: Annotated[int, typer.Option(min=1, help="Passes over the datasets")] = 1,
) -> None:
    """Broker runs per fixture x repeat with progress + ETA; writes data/e2e/e2e-<utc>.json."""
    from bourse.broker.runner import e2e_all, e2e_summary
    from bourse.stack.launcher import table

    settings = get_settings()

    async def run() -> int:
        names = tuple(d.strip() for d in datasets.split(",") if d.strip())
        rows, out, peak = await e2e_all(
            settings, names, echo=lambda line: typer.echo(line), repeat=repeat
        )
        header = ["dataset", "rep", "run_id", "status", "planner", "graded", "fallbacks", "tokens"]
        body = [
            [
                r.dataset,
                str(r.repeat),
                r.run_id,
                r.status,
                r.planner_used,
                f"{r.subtasks_passed}/{r.subtasks_total}",
                str(r.fallbacks),
                f"{r.tokens_in}/{r.tokens_out}",
            ]
            for r in rows
        ]
        typer.echo(table([header, *body]))
        for line in e2e_summary(rows, peak, settings.bourse_resident_limit_bytes):
            typer.echo(line)
        typer.echo(f"written: {out}")
        ok = all(r.status == "SUCCEEDED" for r in rows)
        return 0 if ok and peak <= settings.bourse_resident_limit_bytes else 1

    raise typer.Exit(asyncio.run(run()))


@usage_app.command("maxcall")
def usage_maxcall() -> None:
    """Max, p50, p95 of tokens_in + tokens_out per LLM call, per model (ADR-0033)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.meter.repo_sql import SqlMeterRepository
    from bourse.meter.stats import maxcall_rows, recommend_num_ctx
    from bourse.stack.launcher import table

    settings = get_settings()

    async def show() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            usage = await SqlMeterRepository(engine).list_usage()
        finally:
            await engine.dispose()
        rows = maxcall_rows(usage)
        out = [["model", "calls", "max", "p50", "p95"]]
        out += [[r.model, str(r.calls), str(r.max), str(r.p50), str(r.p95)] for r in rows]
        typer.echo(table(out))
        overall = max((r.max for r in rows), default=0)
        typer.echo(f"max tokens per call {overall}")
        typer.echo(
            f"recommended num_ctx {recommend_num_ctx(overall)} "
            "(smallest of 4096/6144/8192 >= 2 x max)"
        )
        typer.echo(f"configured BOURSE_METER_NUM_CTX {settings.bourse_meter_num_ctx}")
        return 0

    raise typer.Exit(asyncio.run(show()))


@app.command("scores")
def scores_cmd() -> None:
    """Reliability (Wilson 95% LB from spot-checked ledger rows) and EWMA cost/latency."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.broker.reliability import scores
    from bourse.ledger.repo_sql import SqlLedgerRepository
    from bourse.stack.launcher import table

    settings = get_settings()

    async def show() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            rows = await SqlLedgerRepository(engine).list_rows()
        finally:
            await engine.dispose()
        out = [["agent", "skill", "major", "s", "f", "wilson_lb", "ewma_usd", "ewma_wall_s"]]
        for sc in scores(rows, settings.bourse_select_ewma_alpha):
            out.append(
                [
                    sc.agent_id,
                    sc.skill.value,
                    str(sc.agent_major),
                    str(sc.s),
                    str(sc.f),
                    f"{sc.wilson_lb:.4f}",
                    "-" if sc.ewma_usd is None else f"{sc.ewma_usd:.8f}",
                    "-" if sc.ewma_wall_ms is None else f"{sc.ewma_wall_ms / 1000:.1f}",
                ]
            )
        typer.echo(table(out))
        typer.echo(f"ledger rows {len(rows)}")
        return 0

    raise typer.Exit(asyncio.run(show()))


@ledger_app.command("show")
def ledger_show(run: Annotated[str, typer.Option("--run", help="Run id")]) -> None:
    """Ledger rows of one run."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.ledger.repo_sql import SqlLedgerRepository
    from bourse.stack.launcher import table

    settings = get_settings()

    async def show() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            rows = await SqlLedgerRepository(engine).list_rows(run_id=run)
        finally:
            await engine.dispose()
        out = [
            [
                "subtask",
                "att",
                "agent",
                "skill",
                "state",
                "tokens",
                "tools",
                "shadow_usd",
                "spot",
                "wall_s",
            ]
        ]
        for r in rows:
            out.append(
                [
                    r.subtask_id,
                    str(r.attempt),
                    r.agent_id,
                    r.skill.value,
                    r.state.value,
                    f"{r.tokens_in}/{r.tokens_out}",
                    str(r.tool_calls),
                    f"{r.shadow_usd:.8f}",
                    "-" if r.spot_check_passed is None else str(r.spot_check_passed),
                    f"{r.wall_ms / 1000:.1f}",
                ]
            )
        typer.echo(table(out))
        typer.echo(f"ledger rows {len(rows)}")
        return 0 if rows else 1

    raise typer.Exit(asyncio.run(show()))


@ledger_app.command("export")
def ledger_export(
    out: Annotated[Path | None, typer.Option(help="Parquet path")] = None,
) -> None:
    """All ledger rows to parquet, validated by the pandera schema first."""
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.ledger.export import export_parquet
    from bourse.ledger.repo_sql import SqlLedgerRepository

    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out or Path("data/exports") / f"ledger-{stamp}.parquet"

    async def export() -> int:
        engine = create_async_engine(settings.bourse_database_url)
        try:
            rows = await SqlLedgerRepository(engine).list_rows()
        finally:
            await engine.dispose()
        n = export_parquet(rows, target)
        typer.echo(f"exported {n} rows: {target}")
        return 0

    raise typer.Exit(asyncio.run(export()))


if __name__ == "__main__":
    app()
