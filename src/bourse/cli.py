"""`bourse` command line: doctor, probe."""

import asyncio
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer

from bourse import __version__
from bourse.config import get_settings
from bourse.llm.ollama import DEFAULT_HTTP_TIMEOUT
from bourse.llm.probe import run_probe
from bourse.registry.attacks import DEFAULT_FIXTURES, format_table, run_phase1
from bourse.stack.launcher import database_url as resolve_db_url

A2A_SPEC_VERSION = "1.0"
MCP_SPEC_VERSION = "2026-07-28"

DOCTOR_PACKAGES = ("a2a-sdk", "mcp", "langgraph", "langchain-core", "pydantic")

app = typer.Typer(no_args_is_help=True, add_completion=False)
keys_app = typer.Typer(no_args_is_help=True, help="Vendor signing keys.")
card_app = typer.Typer(no_args_is_help=True, help="Sign and verify Agent Cards.")
registry_app = typer.Typer(no_args_is_help=True, help="Agent registry service.")
attacks_app = typer.Typer(no_args_is_help=True, help="Deterministic attack suites.")
adversary_app = typer.Typer(no_args_is_help=True, help="Benchmark-only adversary agents.")
mcp_app = typer.Typer(no_args_is_help=True, help="Pure MCP tool servers.")
meter_app = typer.Typer(no_args_is_help=True, help="Metering LLM proxy.")
agent_app = typer.Typer(no_args_is_help=True, help="A2A agents.")
stack_app = typer.Typer(no_args_is_help=True, help="Local stack of all services.")
smoke_app = typer.Typer(no_args_is_help=True, help="Smoke runs against the local stack.")
runs_app = typer.Typer(no_args_is_help=True, help="Broker run records.")
e2e_app = typer.Typer(no_args_is_help=True, help="Broker end-to-end runs on fixtures.")
usage_app = typer.Typer(no_args_is_help=True, help="Metered LLM usage.")
ledger_app = typer.Typer(no_args_is_help=True, help="Delegation ledger.")
egress_app = typer.Typer(no_args_is_help=True, help="Egress control (compose).")
exam_app = typer.Typer(no_args_is_help=True, help="Admission exams.")
db_app = typer.Typer(no_args_is_help=True, help="Database helpers.")
trust_app = typer.Typer(no_args_is_help=True, help="Trust / reinstatement (ADR-0048).")
approve_app = typer.Typer(no_args_is_help=True, help="Human approval (ADR-0048).")
approver_app = typer.Typer(no_args_is_help=True, help="Compose Slack approver service (ADR-0048).")
app.add_typer(keys_app, name="keys")
app.add_typer(card_app, name="card")
app.add_typer(registry_app, name="registry")
app.add_typer(attacks_app, name="attacks")
app.add_typer(adversary_app, name="adversary")
app.add_typer(mcp_app, name="mcp")
app.add_typer(meter_app, name="meter")
app.add_typer(agent_app, name="agent")
app.add_typer(stack_app, name="stack")
app.add_typer(smoke_app, name="smoke")
app.add_typer(runs_app, name="runs")
app.add_typer(e2e_app, name="e2e")
app.add_typer(usage_app, name="usage")
app.add_typer(ledger_app, name="ledger")
app.add_typer(egress_app, name="egress")
app.add_typer(exam_app, name="exam")
app.add_typer(db_app, name="db")
app.add_typer(trust_app, name="trust")
app.add_typer(approve_app, name="approve")
app.add_typer(approver_app, name="approver")


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
    latency: Annotated[
        int | None,
        typer.Option(
            "--latency",
            help=(
                "N isolated /api/chat samples per model; print p50/p95 and write "
                "latency-latest.json"
            ),
        ),
    ] = None,
    memory: Annotated[
        bool,
        typer.Option(
            "--memory",
            help=(
                "Measure resident bytes for candidate pairs + throwaway gemma4:e4b exams; "
                "write ADR-0045"
            ),
        ),
    ] = False,
) -> None:
    """Probe the local Ollama server and write data/probe/probe-<utc>.json.

    With `--latency N`, also measure isolated latency for large+small models (ADR-0044).
    With `--memory`, measure pair resident bytes and throwaway e4b exams (ADR-0045).
    """
    settings = get_settings()
    if memory:
        from bourse.llm.memory_probe import run_memory_probe

        path = asyncio.run(run_memory_probe(settings))
        typer.echo(f"wrote {path}")
        raise typer.Exit(0)
    chosen = model or settings.bourse_model_small
    if latency is not None:
        if latency < 1:
            raise typer.BadParameter("--latency N must be >= 1")
        code = asyncio.run(_probe_latency(settings, latency, Path(out_dir)))
        raise typer.Exit(code)
    code = asyncio.run(_probe(settings.ollama_base_url, chosen, Path(out_dir)))
    raise typer.Exit(code)


async def _probe_latency(settings: Any, n: int, out_dir: Path) -> int:
    from bourse.llm.latency import measure_latency, write_latency_report

    models = list(dict.fromkeys([settings.bourse_model_small, settings.bourse_model_large]))
    typer.echo(
        f"latency N={n} models={models} "
        f"OLLAMA_NUM_PARALLEL(config)={settings.ollama_num_parallel} "
        f"(host OLLAMA_NUM_PARALLEL is not exposed by the Ollama HTTP API)"
    )
    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        summaries = await measure_latency(client, settings.ollama_base_url, models, n=n)
    path = write_latency_report(
        summaries,
        base_url=settings.ollama_base_url,
        base_deadline_s=settings.bourse_exam_deadline_s,
        out_dir=out_dir,
    )
    for model, s in summaries.items():
        deadline = s.exam_deadline_s(settings.bourse_exam_deadline_s)
        typer.echo(
            f"{model}: n={s.n} "
            f"total_s p50={s.p50_total_s:.2f} p95={s.p95_total_s:.2f} "
            f"load_s p50={s.p50_load_s:.2f} p95={s.p95_load_s:.2f} "
            f"eval_s p50={s.p50_eval_s:.2f} p95={s.p95_eval_s:.2f} "
            f"exam_deadline_s=max(base={settings.bourse_exam_deadline_s:.0f}, "
            f"3*p95)={deadline:.1f}"
        )
    typer.echo(f"written: {path}")
    typer.echo(f"latest: {out_dir / 'latency-latest.json'}")
    return 0


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
    host: Annotated[str, typer.Option(help="Default: BOURSE_BIND_HOST")] = "",
    port: Annotated[int, typer.Option()] = 55300,
) -> None:
    """Serve the registry API against Postgres; admission exams run in the background."""
    import uvicorn
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.registry.app import create_app
    from bourse.registry.repo_sql import SqlRepository
    from bourse.registry.service import RegistryPolicy, RegistryService

    settings = get_settings()
    engine = create_async_engine(resolve_db_url(settings))
    client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    from bourse.approval.select import select_approver
    from bourse.approval.triggers import wilson_lb_from_rows
    from bourse.ledger.repo_sql import SqlLedgerRepository
    from bourse.trust.engine import TrustEngine

    repo = SqlRepository(engine)
    ledger_repo = SqlLedgerRepository(engine)

    async def _wilson(agent_id: str) -> float:
        agent = await repo.get_agent(agent_id)
        if agent is None:
            return 0.0
        rows = await ledger_repo.list_rows()
        return wilson_lb_from_rows(rows, agent_id, agent.current_version)

    trust = TrustEngine(
        repo,
        k=settings.bourse_trust_strikes,
        window_s=settings.bourse_trust_window_s,
        approver=select_approver(settings, tty=False, prefer_http=True),
        wilson_lb_for=_wilson,
    )
    service = RegistryService(
        repo,
        client,
        RegistryPolicy(dev_hosts=settings.dev_hosts),
        strike_k=settings.bourse_trust_strikes,
        strike_window_s=settings.bourse_trust_window_s,
        inline_per_hour=settings.bourse_inline_violations_per_hour,
        engine=trust,
    )
    exams = None
    secret = settings.bourse_honeytoken_secret.get_secret_value().encode()
    if secret:
        from bourse.agents.catalog import specs_from_settings
        from bourse.broker.delegate import MeterBudgetClient
        from bourse.llm.latency import load_deadline_by_model
        from bourse.meter.repo_sql import SqlMeterRepository
        from bourse.registry.service import make_agent_id
        from bourse.stack.launcher import BROKER_METER_ID, meter_key
        from bourse.telemetry import setup_tracing, traced_async_client
        from bourse.trust.exam import ExamConfig, ExamRunner, ExamScheduler
        from bourse.trust.repo import SqlHoneytokenRepository

        # Prefer env (compose embeds probe results); fall back to latency-latest.json (host).
        deadline_by_model = dict(settings.exam_deadline_by_model)
        if not deadline_by_model:
            deadline_by_model = load_deadline_by_model(
                base_deadline_s=settings.bourse_exam_deadline_s
            )
        max_task_deadline = max(
            [settings.bourse_exam_deadline_s, *deadline_by_model.values()],
            default=settings.bourse_exam_deadline_s,
        )
        if deadline_by_model:
            typer.echo(
                "exam deadlines (max(base, 3*p95)): "
                + ", ".join(f"{m}={d:.1f}s" for m, d in sorted(deadline_by_model.items()))
            )
        else:
            typer.echo(
                "exam deadlines: base only "
                f"({settings.bourse_exam_deadline_s:.0f}s); "
                "run `bourse probe --latency 5` for per-model 3*p95"
            )
        id_to_model = {
            make_agent_id(spec.display_name, spec.vendor_domain): spec.model
            for spec in specs_from_settings(settings).values()
        }
        setup_tracing("bourse-registry", settings.otel_exporter_otlp_endpoint)
        exam_http = traced_async_client(timeout=httpx.Timeout(max_task_deadline + 30))
        # Exam delegations need the same per-task meter budgets as the broker (ADR-0041).
        broker_meter = settings.bourse_agent_key.get_secret_value() or meter_key(BROKER_METER_ID)
        usage_repo = SqlMeterRepository(engine)
        runner = ExamRunner(
            service.repo,
            exam_http,
            SqlHoneytokenRepository(engine),
            secret,
            ExamConfig(
                k=settings.bourse_exam_k,
                pass_n=settings.bourse_exam_pass,
                cooldown_s=settings.bourse_exam_cooldown_s,
                deadline_s=settings.bourse_exam_deadline_s,
                exam_timeout_s=settings.bourse_exam_timeout_s,
                deadline_by_model=deadline_by_model,
                contention_queue_fraction=settings.bourse_contention_queue_fraction,
            ),
            budgets=MeterBudgetClient(exam_http, settings.bourse_meter_url, broker_meter),
            task_max_tokens=settings.bourse_task_max_tokens,
            task_max_calls=settings.bourse_task_max_calls,
            model_for_agent=lambda aid: id_to_model.get(aid, settings.bourse_model_small),
            usage_lookup=lambda task_id: usage_repo.list_usage(task_id=task_id),
            echo=typer.echo,
        )
        typer.echo(
            f"exam concurrency limit={settings.ollama_num_parallel} (OLLAMA_NUM_PARALLEL; ADR-0049)"
        )
        exams = ExamScheduler(
            runner,
            max_concurrent=settings.ollama_num_parallel,
            echo=typer.echo,
        )
    app_ = create_app(
        service,
        settings.bourse_admin_token.get_secret_value(),
        exams,
        rate_per_min=settings.bourse_register_rate_per_min,
    )
    uvicorn.run(app_, host=host or settings.bourse_bind_host, port=port)


@attacks_app.command("phase1")
def attacks_phase1(
    fixtures: Annotated[str, typer.Option(help="Card fixture directory")] = str(DEFAULT_FIXTURES),
) -> None:
    """Run A1/A1b/A2/A3/A9 and 3 honest controls in memory; print n/N."""
    results = asyncio.run(run_phase1(Path(fixtures)))
    typer.echo(format_table(results))
    raise typer.Exit(0 if all(r.caught for r in results) else 1)


@attacks_app.command("phase5")
def attacks_phase5(
    runs: Annotated[int, typer.Option(help="e2e runs per ACTIVE adversary")] = 6,
    only: Annotated[str, typer.Option(help="Comma-separated scenario ids")] = "",
    mode: Annotated[
        str,
        typer.Option(help="Filter modes: mixed, sole-provider, card (empty = all)"),
    ] = "",
    reset_honest: Annotated[
        bool,
        typer.Option(
            "--reset-honest",
            help="Reinstates quarantined honest agents to ACTIVE before this invocation only",
        ),
    ] = False,
    reuse_agent: Annotated[
        bool,
        typer.Option(
            "--reuse-agent",
            help="A5a/A5b: reuse ACTIVE registered identity (no re-register); for re-catch",
        ),
    ] = False,
) -> None:
    """Adversary suite A4-A8 plus A1-A3,A9 over compose card_url (ADR-0042)."""
    from bourse.adversaries.runner import (
        InfraNotReady,
        InvariantViolation,
        format_phase5,
        red,
        run_phase5,
        write_report,
    )
    from bourse.stack.compose_guard import refuse_if_compose_host

    refuse_if_compose_host("attacks phase5")

    settings = get_settings()
    ids = [p.strip() for p in only.split(",") if p.strip()] or None
    modes = [p.strip() for p in mode.split(",") if p.strip()] or None

    def _infra_message(exc: BaseException) -> str | None:
        if isinstance(exc, InfraNotReady):
            return exc.check
        if isinstance(exc, BaseExceptionGroup):
            for sub in exc.exceptions:
                msg = _infra_message(sub)
                if msg is not None:
                    return msg
        return None

    async def go() -> int:
        try:
            report = await run_phase5(
                settings,
                runs=runs,
                only=ids,
                modes=modes,
                echo=typer.echo,
                reset_honest=reset_honest,
                reuse_agent=reuse_agent,
            )
        except InvariantViolation as exc:
            typer.echo(f"INVARIANT FAIL: {exc}", err=True)
            return 1
        except BaseException as exc:
            msg = _infra_message(exc)
            if msg is None:
                raise
            typer.echo(f"INFRA NOT READY: {msg}", err=True)
            return 1
        path = await write_report(report)
        typer.echo(format_phase5(report))
        typer.echo(f"written {path}")
        return 1 if red(report) else 0

    raise typer.Exit(asyncio.run(go()))


@adversary_app.command("serve")
def adversary_serve(
    name: Annotated[str, typer.Argument(help="Adversary name or card-attacks")],
) -> None:
    """Serve one benchmark-only adversary (compose profile adversaries)."""
    from bourse.adversaries.agent import serve_adversary
    from bourse.adversaries.catalog import ADVERSARY_NAMES, CARD_ATTACKS_NAME

    if name not in (*ADVERSARY_NAMES, CARD_ATTACKS_NAME):
        raise typer.BadParameter(f"unknown adversary {name}")
    settings = get_settings()
    key = settings.bourse_agent_key.get_secret_value()
    serve_adversary(name, settings, settings.bourse_meter_url, key)


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
        repo=SqlMeterRepository(create_async_engine(resolve_db_url(settings))),
        config=MeterConfig(
            max_tokens_per_call=settings.bourse_meter_max_tokens_per_call,
            deadline_s=settings.bourse_meter_deadline_s,
            sampling=Sampling(
                temperature=settings.bourse_meter_temperature,
                seed=settings.bourse_meter_seed,
                keep_alive=settings.bourse_meter_keep_alive,
            ),
            ollama_base_url=settings.ollama_base_url,
            require_task_budget=True,
            max_in_flight=settings.bourse_meter_max_in_flight,
            tokens_per_min=settings.bourse_meter_tokens_per_min,
            max_prompt_tokens=settings.bourse_meter_max_prompt_tokens,
            queue_timeout_s=settings.bourse_meter_queue_timeout_s,
            global_parallel=settings.ollama_num_parallel,
        ),
        upstream_client=client,
    )
    uvicorn.run(app_, host=settings.bourse_bind_host, port=port)


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
def stack_up(
    mode: Annotated[str, typer.Option(help="compose (default) or host")] = "compose",
    with_adversaries: Annotated[
        bool,
        typer.Option(
            "--with-adversaries",
            help="After honest agents are registered and ACTIVE, start the adversaries profile",
        ),
    ] = False,
) -> None:
    """compose: isolated containers with egress control (ADR-0034); host: local processes.

    `--with-adversaries` registers honest agents, waits until 5/5 ACTIVE, then starts the
    adversaries profile. Do not use raw `docker compose --profile adversaries up`.
    """
    from bourse.stack.launcher import (
        HOST_MODE_WARNING,
        compose,
        services,
        set_mode,
        start,
        table,
        wait_healthy,
        write_compose_env,
    )

    settings = get_settings()
    if mode not in ("compose", "host"):
        raise typer.BadParameter("mode must be compose or host")
    if with_adversaries and mode != "compose":
        raise typer.BadParameter("--with-adversaries requires --mode compose")
    if mode == "compose":
        from bourse.stack.image_freshness import REBUILD_HINT, stale_compose_images

        stale = stale_compose_images()
        if stale:
            typer.echo(
                "stale compose image(s); "
                + REBUILD_HINT
                + " first:\n"
                + "\n".join(f"  {s.line()}" for s in stale),
                err=True,
            )
            raise typer.Exit(1)
    set_mode(mode)
    if mode == "compose":
        from bourse.stack.activation import dump_activation_diagnostics, wait_agents_active
        from bourse.stack.launcher import verify_bourse_gone
        from bourse.stack.register import register_all

        left_c, left_n = verify_bourse_gone()
        if left_c or left_n:
            typer.echo(
                "leftover bourse state; run `uv run bourse stack down` first:\n"
                + "\n".join(
                    [*(f"  container {c}" for c in left_c), *(f"  network {n}" for n in left_n)]
                ),
                err=True,
            )
            raise typer.Exit(1)
        written = write_compose_env(settings)
        typer.echo(f"env files {len(written)} (data/run/env/)")
        # Honest stack only; adversaries start after ACTIVE when --with-adversaries.
        result = compose("up", "-d", "--remove-orphans", check=False)
        typer.echo((result.stdout + result.stderr).strip())
        if result.returncode != 0:
            raise typer.Exit(result.returncode)
        code = _wait_compose_healthy(timeout_s=300.0)
        if code != 0:
            raise typer.Exit(code)
        from bourse.stack.activation import _sample_ollama_resident

        _sample_ollama_resident(settings, echo=typer.echo, warn_roster=True)
        if not with_adversaries:
            raise typer.Exit(0)
        rows, accepted, total = register_all(settings, "compose")
        typer.echo(table(rows))
        typer.echo(f"registered {accepted}/{total} ACCEPTED")
        if accepted != total:
            dump_activation_diagnostics(typer.echo)
            raise typer.Exit(1)
        if wait_agents_active(settings, echo=typer.echo) != 0:
            dump_activation_diagnostics(typer.echo)
            raise typer.Exit(1)
        adv = compose("--profile", "adversaries", "up", "-d", "--remove-orphans", check=False)
        typer.echo((adv.stdout + adv.stderr).strip())
        if adv.returncode != 0:
            raise typer.Exit(adv.returncode)
        raise typer.Exit(_wait_compose_healthy(timeout_s=300.0))
    typer.echo(HOST_MODE_WARNING)
    svcs = services(settings)
    pids = {s.name: start(s) for s in svcs}
    health = wait_healthy(svcs)
    rows = [["name", "port", "pid", "healthy"]]
    rows += [[s.name, str(s.port), str(pids[s.name]), str(health[s.name])] for s in svcs]
    typer.echo(table(rows))
    up = sum(health.values())
    typer.echo(f"healthy {up}/{len(svcs)} (logs: data/logs/<name>.log)")
    typer.echo(HOST_MODE_WARNING)
    raise typer.Exit(0 if up == len(svcs) else 1)


@stack_app.command("down")
def stack_down() -> None:
    """Stop the stack: every compose profile, volumes, orphans, and one-off runs."""
    from bourse.stack.launcher import (
        compose_profiles,
        current_mode,
        services,
        stack_compose_down,
        stop,
        verify_bourse_gone,
    )

    if current_mode() == "compose":
        profiles = compose_profiles()
        typer.echo("profiles " + (" ".join(profiles) if profiles else "(none)"))
        result = stack_compose_down()
        typer.echo((result.stdout + result.stderr).strip())
        left_c, left_n = verify_bourse_gone()
        if left_c or left_n:
            for c in left_c:
                typer.echo(f"leftover container {c}", err=True)
            for n in left_n:
                typer.echo(f"leftover network {n}", err=True)
            typer.echo("compose down FAILED: leftover bourse state", err=True)
            raise typer.Exit(1)
        typer.echo("compose down ok (no bourse containers/networks)")
        raise typer.Exit(0 if result.returncode == 0 else result.returncode)
    svcs = services(get_settings())
    stopped = sum(stop(s.name) for s in svcs)
    typer.echo(f"stopped {stopped}/{len(svcs)}")


def _registry_states() -> list[list[str]]:
    from bourse.stack.activation import registry_agent_rows

    settings = get_settings()
    base = settings.bourse_registry_url.rstrip("/")
    rows = [["agent", "state", "last transition"]]
    try:
        with httpx.Client(timeout=10.0) as client:
            rows.extend([list(r) for r in registry_agent_rows(client, base)])
    except httpx.HTTPError as exc:
        rows.append(["registry unreachable", type(exc).__name__, "-"])
    return rows


def _service_ok(c: dict[str, Any]) -> bool:
    health = str(c.get("Health") or "-")
    state = str(c.get("State") or "-")
    if c.get("Service") == "migrate":
        return state == "exited" and "exited (0)" in str(c.get("Status", "")).lower()
    return state == "running" and health in ("healthy", "-")


def _compose_status() -> int:
    from bourse.stack.launcher import compose_ps, table

    ps = compose_ps()
    rows = [["service", "state", "health"]]
    ok = 0
    for c in sorted(ps, key=lambda c: str(c.get("Service"))):
        ok += _service_ok(c)
        rows.append(
            [str(c.get("Service")), str(c.get("State") or "-"), str(c.get("Health") or "-")]
        )
    typer.echo(table(rows))
    typer.echo(f"services ok {ok}/{len(ps)}")
    return 0 if ps and ok == len(ps) else 1


def _unexpected_exit(c: dict[str, Any]) -> bool:
    """True when a service has exited or is unhealthy (not migrate with code 0)."""
    if c.get("Service") == "migrate":
        return False
    state = str(c.get("State") or "")
    health = str(c.get("Health") or "-")
    return state == "exited" or health == "unhealthy"


def _print_approver_health_reason() -> None:
    """Fetch published approver /healthz and print slack status (never token values)."""
    from bourse.approval.redact import redact_secrets
    from bourse.stack.topology import APPROVER_PORT

    url = f"http://127.0.0.1:{APPROVER_PORT}/healthz"
    try:
        resp = httpx.get(url, timeout=2.0)
        try:
            body = resp.json()
        except ValueError:
            typer.echo(
                f"approver /healthz status={resp.status_code} body_unparseable",
                err=True,
            )
            return
        slack = body.get("slack", "?")
        detail = redact_secrets(str(body.get("detail", "") or ""))
        typer.echo(
            f"approver /healthz slack={slack} detail={detail or '(none)'}",
            err=True,
        )
    except httpx.HTTPError as exc:
        typer.echo(f"approver /healthz unreachable: {type(exc).__name__}", err=True)


CREATED_TERMINAL_S = 20.0


def _wait_compose_healthy(timeout_s: float) -> int:
    """Poll `docker compose ps` until every service is running+healthy (migrate: exited 0).

    State `created` for > CREATED_TERMINAL_S is terminal (usually static-IP / orphan conflict).
    """
    import time

    from bourse.stack.launcher import compose, compose_ps

    started = time.monotonic()
    created_since: dict[str, float] = {}
    while True:
        elapsed = time.monotonic() - started
        ps = compose_ps()
        dead = [c for c in ps if _unexpected_exit(c)]
        if dead:
            c = dead[0]
            name = str(c.get("Service"))
            typer.echo(f"service {name} failed: {c.get('Status')}", err=True)
            if name == "approver":
                _print_approver_health_reason()
            logs = compose("logs", "--tail", "30", name, check=False)
            typer.echo((logs.stdout + logs.stderr).rstrip())
            return 1
        now = time.monotonic()
        still_created: list[str] = []
        for c in ps:
            name = str(c.get("Service"))
            state = str(c.get("State") or "").lower()
            if state == "created":
                created_since.setdefault(name, now)
                if now - created_since[name] > CREATED_TERMINAL_S:
                    still_created.append(name)
            else:
                created_since.pop(name, None)
        if still_created:
            name = still_created[0]
            typer.echo(
                f"service {name} stuck in created >{CREATED_TERMINAL_S:.0f}s "
                "(static IP conflict / orphan?). Use: docker compose down -v --remove-orphans",
                err=True,
            )
            logs = compose("logs", "--tail", "30", name, check=False)
            typer.echo((logs.stdout + logs.stderr).rstrip() or "(no logs; container never started)")
            detail = compose("ps", "-a", name, check=False)
            typer.echo((detail.stdout or detail.stderr or "").strip()[:500])
            return 1
        pending = [str(c.get("Service")) for c in ps if not _service_ok(c)]
        typer.echo(f"[{elapsed:4.0f}s] services ok {len(ps) - len(pending)}/{len(ps)}")
        if ps and not pending:
            return _compose_status()
        if elapsed >= timeout_s:
            if "approver" in pending:
                _print_approver_health_reason()
            return _compose_status()
        time.sleep(5)


@stack_app.command("status")
def stack_status() -> None:
    """Service health and each registered agent's trust state and last transition."""
    from bourse.stack.launcher import (
        HOST_MODE_WARNING,
        current_mode,
        healthy,
        read_pid,
        services,
        table,
    )

    if current_mode() == "compose":
        code = _compose_status()
    else:
        typer.echo(HOST_MODE_WARNING)
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
        code = 0 if ok == len(svcs) else 1
    states = _registry_states()
    typer.echo(table(states))
    active = sum(r[1] == "ACTIVE" for r in states[1:])
    typer.echo(f"agents ACTIVE {active}/{len(states) - 1}")
    raise typer.Exit(code)


@stack_app.command("register")
def stack_register() -> None:
    """Onboard vendor-a/b/c with pinned JWKS and register the 5 agents by card_url.

    In compose mode the registry starts an admission exam for each new PENDING agent."""
    from bourse.stack.launcher import current_mode, table
    from bourse.stack.register import register_all

    rows, accepted, total = register_all(get_settings(), current_mode())
    typer.echo(table(rows))
    typer.echo(f"registered {accepted}/{total} ACCEPTED")
    raise typer.Exit(0 if accepted == total else 1)


@stack_app.command("wait-active")
def stack_wait_active(
    timeout: Annotated[
        float | None,
        typer.Option(
            help="Seconds (default: computed from pending exam task count). "
            "EXAM_FAILED fails immediately."
        ),
    ] = None,
) -> None:
    """Poll until every registered agent is ACTIVE; dump diagnostics on failure."""
    from bourse.stack.activation import dump_activation_diagnostics, wait_agents_active

    settings = get_settings()
    code = wait_agents_active(settings, timeout_s=timeout, echo=typer.echo)
    if code != 0:
        dump_activation_diagnostics(typer.echo)
    raise typer.Exit(code)


@stack_app.command("compose-file")
def stack_compose_file(
    check: Annotated[
        bool, typer.Option(help="Exit 1 if docker-compose.yml is out of date")
    ] = False,
) -> None:
    """Render docker-compose.yml from bourse.stack.topology (single source)."""
    import yaml

    from bourse.stack.topology import COMPOSE_HEADER, compose_document

    text = COMPOSE_HEADER + yaml.safe_dump(compose_document(), sort_keys=False, width=100)
    target = Path("docker-compose.yml")
    if check:
        same = target.exists() and yaml.safe_load(target.read_text()) == compose_document()
        typer.echo("docker-compose.yml " + ("up to date" if same else "OUT OF DATE"))
        raise typer.Exit(0 if same else 1)
    target.write_text(text)
    typer.echo(f"written: {target}")


@stack_app.command("activate")
def stack_activate(
    all_agents: Annotated[bool, typer.Option("--all", help="Activate every PENDING agent")] = False,
    force_unsafe: Annotated[
        bool, typer.Option("--force-unsafe", help="Bypass the admission exam (audited)")
    ] = False,
) -> None:
    """Admin PENDING -> ACTIVE without the admission exam (ADR-0037; audited as unsafe)."""
    from bourse.stack.launcher import table
    from bourse.stack.register import activate_all

    if not (all_agents and force_unsafe):
        typer.echo(
            "manual activation bypasses the admission exam; pass --all --force-unsafe", err=True
        )
        raise typer.Exit(2)
    rows, activated, total = activate_all(get_settings())
    typer.echo(table(rows))
    typer.echo(f"activated {activated}/{total}")
    raise typer.Exit(0 if activated == total else 1)


@app.command("healthcheck")
def healthcheck(url: Annotated[str, typer.Argument(help="URL; healthy if status < 500")]) -> None:
    """Container healthcheck: reads only the status line (SSE endpoints never finish)."""
    try:
        with httpx.stream("GET", url, timeout=4.0) as resp:
            raise typer.Exit(0 if resp.status_code < 500 else 1)
    except httpx.HTTPError as exc:
        raise typer.Exit(1) from exc


@egress_app.command("dnsmasq-config")
def egress_dnsmasq_config() -> None:
    """Print the egress resolver config generated from the topology (ADR-0035)."""
    from bourse.stack.topology import dnsmasq_conf

    typer.echo(dnsmasq_conf(), nl=False)


@egress_app.command("dns-health")
def egress_dns_health() -> None:
    """Query 127.0.0.1:53: allowlisted name resolves, attacker.example is sinkholed."""
    from bourse.egress.dns_log import dns_health_ok

    problem = dns_health_ok()
    if problem:
        typer.echo(problem, err=True)
        raise typer.Exit(1)
    typer.echo("dns-health ok")


@egress_app.command("dns-watch")
def egress_dns_watch(
    log_path: Annotated[str, typer.Argument(help="dnsmasq --log-facility file")],
) -> None:
    """Tail dnsmasq query log; HARD DNS_EGRESS for agent lookups off the allowlist."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.broker.delegate import RegistryClient
    from bourse.contracts.trust import ViolationReport
    from bourse.egress.dns_log import DnsLogWatcher
    from bourse.stack.launcher import admin_token
    from bourse.stack.topology import agent_ids_by_ip, allowlisted_names, ip_identities
    from bourse.trust.repo import SqlEgressRepository

    settings = get_settings()

    async def serve() -> None:
        engine = create_async_engine(resolve_db_url(settings))
        async with httpx.AsyncClient(timeout=10.0) as http:
            registry = RegistryClient(http, settings.bourse_registry_url, admin_token(settings))

            async def report(agent_id: str, v: ViolationReport) -> None:
                try:
                    await registry.report_violation(agent_id, v)
                except httpx.HTTPError as exc:
                    typer.echo(f"DNS_EGRESS not recorded for {agent_id}: {exc!r}", err=True)

            watcher = DnsLogWatcher(
                identities=ip_identities(),
                agent_ips=agent_ids_by_ip(),
                allow=frozenset(allowlisted_names()),
                repo=SqlEgressRepository(engine),
                report=report,
            )
            typer.echo(f"dns-watch {log_path}")
            await watcher.follow(Path(log_path))

    asyncio.run(serve())


@egress_app.command("sinkhole")
def egress_sinkhole() -> None:
    """Serve the sinkhole on BOURSE_SINKHOLE_PORTS; write egress_events; report violations."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.broker.delegate import RegistryClient
    from bourse.contracts.trust import ViolationReport
    from bourse.egress.sinkhole import Sinkhole
    from bourse.stack.launcher import admin_token
    from bourse.stack.topology import agent_ids_by_ip, ip_identities
    from bourse.trust.repo import SqlEgressRepository, SqlHoneytokenRepository

    settings = get_settings()
    ports = [int(p) for p in settings.bourse_sinkhole_ports.split(",") if p.strip()]

    async def serve() -> None:
        engine = create_async_engine(resolve_db_url(settings))
        async with httpx.AsyncClient(timeout=10.0) as http:
            registry = RegistryClient(http, settings.bourse_registry_url, admin_token(settings))

            async def report(agent_id: str, v: ViolationReport) -> None:
                try:
                    await registry.report_violation(agent_id, v)
                except httpx.HTTPError as exc:
                    typer.echo(f"violation not recorded for {agent_id}: {exc!r}", err=True)

            sinkhole = Sinkhole(
                identities=ip_identities(),
                agent_ips=agent_ids_by_ip(),
                repo=SqlEgressRepository(engine),
                tokens=SqlHoneytokenRepository(engine),
                report=report,
            )
            typer.echo(f"sinkhole listening on {ports}")
            await sinkhole.serve(ports, settings.bourse_bind_host)

    asyncio.run(serve())


@db_app.command("ensure")
def db_ensure() -> None:
    """CREATE DATABASE for BOURSE_DATABASE_URL if it does not exist (compose migrate step)."""
    import psycopg
    from sqlalchemy.engine import make_url

    from bourse.stack.topology import COMPOSE_DB

    url = make_url(resolve_db_url(get_settings()))
    name = url.database or "bourse"
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists is None:
            conn.execute(f'CREATE DATABASE "{name}"')
    typer.echo(f"database {name} " + ("exists" if exists else "created"))
    typer.echo(
        f"llm_usage lives in database={name} (compose migrate uses {COMPOSE_DB}; "
        f"POSTGRES_DB=bourse is empty unless you migrate host mode)"
    )


@exam_app.command("run")
def exam_run(agent_id: Annotated[str, typer.Argument(help="Agent id")]) -> None:
    """Ask the registry to (re)run the admission exam for a PENDING agent."""
    from bourse.stack.launcher import admin_token

    settings = get_settings()
    resp = httpx.post(
        f"{settings.bourse_registry_url.rstrip('/')}/agents/{agent_id}/exam",
        headers={"Authorization": f"Bearer {admin_token(settings)}"},
        timeout=10.0,
    )
    typer.echo(f"HTTP {resp.status_code} {resp.text}")
    raise typer.Exit(0 if resp.status_code == 202 else 1)


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
        engine = create_async_engine(resolve_db_url(settings))
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
            resident = session.monitor.summary() + "\n" + session.monitor.reload_summary()
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
        engine = create_async_engine(resolve_db_url(settings))
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
            resident = session.monitor.summary() + "\n" + session.monitor.reload_summary()
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
        rows, out, stats = await e2e_all(
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
        for line in e2e_summary(rows, stats, settings.bourse_resident_limit_bytes):
            typer.echo(line)
        typer.echo(f"written: {out}")
        ok = all(r.status == "SUCCEEDED" for r in rows)
        return 0 if ok and stats.resident_peak <= settings.bourse_resident_limit_bytes else 1

    raise typer.Exit(asyncio.run(run()))


@usage_app.command("maxcall")
def usage_maxcall() -> None:
    """Max, p50, p95 of tokens_in + tokens_out per LLM call, per model (ADR-0033)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.meter.repo_sql import SqlMeterRepository
    from bourse.meter.stats import NUM_CTX_CHOICES, maxcall_rows, recommend_num_ctx
    from bourse.stack.launcher import table

    settings = get_settings()

    async def show() -> int:
        engine = create_async_engine(resolve_db_url(settings))
        try:
            usage = await SqlMeterRepository(engine).list_usage()
        finally:
            await engine.dispose()
        rows = maxcall_rows(usage)
        out = [["model", "calls", "max", "p50", "p95", "recommended", "configured"]]
        for r in rows:
            out.append(
                [
                    r.model,
                    str(r.calls),
                    str(r.max),
                    str(r.p50),
                    str(r.p95),
                    str(recommend_num_ctx(r.max)),
                    str(settings.num_ctx_for(r.model)),
                ]
            )
        typer.echo(table(out))
        choices = "/".join(str(c) for c in NUM_CTX_CHOICES)
        typer.echo(f"rule: recommended = smallest of {choices} >= 2 x max (per model)")
        typer.echo(f"BOURSE_NUM_CTX_BY_MODEL {settings.bourse_num_ctx_by_model}")
        mismatched = [
            r.model for r in rows if recommend_num_ctx(r.max) != settings.num_ctx_for(r.model)
        ]
        typer.echo(f"models where configured != recommended: {len(mismatched)}")
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
        engine = create_async_engine(resolve_db_url(settings))
        try:
            rows = await SqlLedgerRepository(engine).list_rows()
        finally:
            await engine.dispose()
        from bourse.trust.reliability_epoch import (
            card_major_from_encoded,
            rel_epoch_from_major,
        )

        out = [
            [
                "agent",
                "skill",
                "major",
                "rel_epoch",
                "s",
                "f",
                "wilson_lb",
                "ewma_usd",
                "ewma_wall_s",
            ]
        ]
        for sc in scores(rows, settings.bourse_select_ewma_alpha):
            out.append(
                [
                    sc.agent_id,
                    sc.skill.value,
                    str(card_major_from_encoded(sc.agent_major)),
                    str(rel_epoch_from_major(sc.agent_major)),
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
        engine = create_async_engine(resolve_db_url(settings))
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
        engine = create_async_engine(resolve_db_url(settings))
        try:
            rows = await SqlLedgerRepository(engine).list_rows()
        finally:
            await engine.dispose()
        n = export_parquet(rows, target)
        typer.echo(f"exported {n} rows: {target}")
        return 0

    raise typer.Exit(asyncio.run(export()))


@approver_app.command("serve")
def approver_serve(
    host: Annotated[str, typer.Option(help="Default: BOURSE_BIND_HOST")] = "",
    port: Annotated[int, typer.Option()] = 55310,
    stub_socket: Annotated[
        bool,
        typer.Option(
            "--stub-socket",
            help="In-process Socket Mode stub (no Slack network); for tests",
        ),
    ] = False,
) -> None:
    """SlackApprover Socket Mode + HTTP API on one asyncio loop (ADR-0048)."""
    from bourse.approval.service import serve as serve_approver

    settings = get_settings()
    serve_approver(
        host=host or settings.bourse_bind_host,
        port=port,
        stub_socket=stub_socket,
    )


@approve_app.command("test")
def approve_test(
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait; timeout -> deny")
    ] = 900.0,
    require_slack: Annotated[
        bool,
        typer.Option("--require-slack", help="Exit 1 unless SlackApprover is selected"),
    ] = False,
    expect: Annotated[
        str,
        typer.Option("--expect", help="Operator banner only: approve | deny | none"),
    ] = "approve",
) -> None:
    """Send a structured test approval and wait for Approve/Deny (ADR-0048)."""
    from bourse.approval.facts import make_request
    from bourse.approval.http_client import HttpApprover
    from bourse.approval.select import missing_slack_env, select_host_approver
    from bourse.approval.slack import SlackApprover
    from bourse.contracts.approval import ApprovalFacts, ApprovalTrigger

    settings = get_settings()
    approver = select_host_approver(settings)
    if require_slack and isinstance(approver, HttpApprover):
        health = _approver_slack_health()
        if health != "connected":
            typer.echo(
                f"SlackApprover required (--require-slack); compose approver slack={health}",
                err=True,
            )
            raise typer.Exit(1)
    elif require_slack and not isinstance(approver, SlackApprover):
        missing = missing_slack_env(settings)
        typer.echo(
            "SlackApprover required (--require-slack); "
            f"got {type(approver).__name__}; missing {', '.join(missing) or '(none)'}",
            err=True,
        )
        raise typer.Exit(1)
    facts = ApprovalFacts(
        agent_id="approve-test",
        vendor="bourse.test",
        exam_n=0,
        exam_total=0,
        strikes=0,
        exposure=0,
    )
    req = make_request(ApprovalTrigger.REINSTATE_AGENT, facts)

    async def go() -> int:
        if isinstance(approver, SlackApprover):
            await approver.start()
        try:
            from bourse.trust.reinstate_client import click_banner

            typer.echo(click_banner(req.request_id, req.trigger.value, "approve-test", expect))
            decision = await approver.request(req, timeout)
        finally:
            if isinstance(approver, SlackApprover):
                await approver.stop()
            if isinstance(approver, HttpApprover):
                await approver.aclose()
        typer.echo(
            f"approved={decision.approved} by={decision.by} "
            f"reason={decision.reason} at={decision.at.isoformat()}"
        )
        return 0 if decision.approved else 1

    raise typer.Exit(asyncio.run(go()))


def _approver_slack_health() -> str:
    from bourse.stack.topology import APPROVER_PORT

    try:
        resp = httpx.get(f"http://127.0.0.1:{APPROVER_PORT}/healthz", timeout=5.0)
        return str(resp.json().get("slack", "unknown"))
    except (httpx.HTTPError, ValueError):
        return "unreachable"


@trust_app.command("reinstate")
def trust_reinstate(
    agent_id: Annotated[str, typer.Argument(help="Agent id (must be QUARANTINED)")],
    reason: Annotated[str, typer.Option("--reason", help="Operator reason")] = "",
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Approval wait seconds (default: BOURSE_SLACK_TIMEOUT_S)"),
    ] = None,
    expect: Annotated[
        str,
        typer.Option("--expect", help="Operator banner only: approve | deny | none"),
    ] = "approve",
) -> None:
    """Host CLI: registry reinstate-exam -> Slack approval -> registry apply (ADR-0048)."""
    from bourse.approval.http_client import HttpApprover
    from bourse.approval.select import select_host_approver
    from bourse.approval.slack import SlackApprover
    from bourse.stack.launcher import admin_token
    from bourse.trust.reinstate_client import host_reinstate

    if not reason.strip():
        raise typer.BadParameter("--reason is required")
    settings = get_settings()
    timeout_s = float(timeout) if timeout is not None else float(settings.bourse_slack_timeout_s)
    base = settings.bourse_registry_url.rstrip("/")
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    approver = select_host_approver(settings)

    async def go() -> int:
        if isinstance(approver, SlackApprover):
            await approver.start()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as http:
                result = await host_reinstate(
                    http,
                    base,
                    headers,
                    agent_id,
                    approver,
                    reason=reason,
                    timeout_s=timeout_s,
                    echo=typer.echo,
                    expect=expect,
                )
        finally:
            if isinstance(approver, SlackApprover):
                await approver.stop()
            if isinstance(approver, HttpApprover):
                await approver.aclose()
        return result.rc

    raise typer.Exit(asyncio.run(go()))


if __name__ == "__main__":
    app()
