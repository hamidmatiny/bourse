"""`bourse bench ...` commands (ADR-0051)."""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer
import yaml

from bourse.bench import host
from bourse.bench.final import (
    FinalRefused,
    dev_provenance,
    final_guard_from_preflight,
    final_preflight,
    ollama_env,
    resident_digests,
    write_lock,
    write_preflight,
)
from bourse.bench.harness import BENCH_DIR, BenchRefused, load_records, run_bench
from bourse.bench.report import format_summary, sizing_lines, summarize
from bourse.bench.split import SplitError, read_manifest, verify_cases, write_manifest
from bourse.config import Settings, get_settings
from bourse.contracts.bench import BenchConfig, Provenance

bench_app = typer.Typer(no_args_is_help=True, help="BOURSE-Bench harness (ADR-0051).")
split_app = typer.Typer(no_args_is_help=True, help="Frozen dev/test split manifests.")
bench_app.add_typer(split_app, name="split")

SplitOpt = Annotated[Literal["dev", "test"], typer.Option("--split", help="dev or test")]


def _refuse_on_preconditions(cfg: BenchConfig, settings: Settings, who: str) -> None:
    """Exit 2 with every problem listed if the agents this config needs are not ACTIVE."""
    from bourse.agents.catalog import specs_from_settings
    from bourse.bench.preflight import check

    problems = check(cfg, settings.bourse_registry_url, len(specs_from_settings(settings)))
    if problems:
        typer.echo(f"REFUSED: {who} preconditions failed ({len(problems)}):", err=True)
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(2)


def _all_error_line(records: list[Any], ran: int) -> list[str]:
    """A run whose every cell this invocation ended in ERROR is an infrastructure failure."""
    from bourse.contracts.bench import CellResult, PiiCaseResult

    recent = [r for r in records if isinstance(r, CellResult | PiiCaseResult)][-ran:] if ran else []
    errored = [
        r
        for r in recent
        if (isinstance(r, CellResult) and r.status == "ERROR")
        or (isinstance(r, PiiCaseResult) and r.state == "ERROR")
    ]
    if recent and len(errored) == len(recent):
        return [f"BENCH RUN FAILED: all {len(recent)} cells run this invocation ended in ERROR"]
    return []


def load_config(path: Path) -> BenchConfig:
    data = yaml.safe_load(path.read_text())
    return BenchConfig.model_validate(data)


def _fail(msg: str) -> None:
    typer.echo(f"REFUSED: {msg}", err=True)
    raise typer.Exit(2)


@split_app.command("write")
def split_write(
    split: SplitOpt,
    n: Annotated[int, typer.Option("--n", help="Number of cases", min=1)],
    seed: Annotated[str, typer.Option("--seed", help="Base seed (recorded in the manifest)")],
    overwrite_dev: Annotated[
        bool, typer.Option("--overwrite-dev", help="Rewrite the dev manifest (never test)")
    ] = False,
) -> None:
    """Write bench/manifests/<split>.json and pin its sha256. The test split is written once."""
    try:
        path = write_manifest(split, n, seed, overwrite_dev=overwrite_dev)
    except SplitError as exc:
        _fail(str(exc))
        return
    manifest, sha = read_manifest(split)
    domains: dict[str, int] = {}
    for c in manifest.cases:
        domains[c.domain] = domains.get(c.domain, 0) + 1
    typer.echo(f"wrote {path} cases={len(manifest.cases)} sha256={sha}")
    typer.echo("domains: " + ", ".join(f"{d} {k}" for d, k in sorted(domains.items())))


@split_app.command("verify")
def split_verify(split: SplitOpt) -> None:
    """Check the pinned manifest hash and regenerate every case against its sha256."""
    try:
        manifest, sha = read_manifest(split)
        cases = verify_cases(manifest)
    except SplitError as exc:
        _fail(str(exc))
        return
    typer.echo(
        f"{split}: manifest sha256 {sha} ok; cases regenerated {len(cases)}/{len(manifest.cases)} "
        f"generator={manifest.generator_version} grader={manifest.grader_version}"
    )


async def _fetch_ps(http: httpx.AsyncClient, settings: Settings) -> Mapping[str, Any]:
    key = settings.bourse_ollama_ps_key.get_secret_value()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    resp = await http.get(settings.bourse_ollama_ps_url, headers=headers, timeout=10.0)
    resp.raise_for_status()
    data: Mapping[str, Any] = resp.json()
    return data


@bench_app.command("preflight")
def bench_preflight(
    config: Annotated[Path, typer.Option("--config", help="Bench config YAML (split: test)")],
) -> None:
    """Host side of --final: clean git tree, pinned test manifest, model digests from /api/ps."""
    from bourse.stack.compose_guard import in_compose_network

    if in_compose_network():
        _fail("bench preflight runs on the host (it needs .git)")
    settings = get_settings()
    cfg = load_config(config)

    async def go() -> Provenance:
        async with httpx.AsyncClient() as http:
            _guard, prov = await final_preflight(
                cfg,
                roster=settings.roster_models,
                fetch_ps=lambda: _fetch_ps(http, settings),
                env=ollama_env(settings.ollama_base_url, settings.ollama_num_parallel),
            )
            return prov

    try:
        prov = asyncio.run(go())
    except FinalRefused as exc:
        _fail(str(exc))
        return
    path = write_preflight(BENCH_DIR / cfg.name, prov)
    typer.echo(
        f"preflight ok: git {prov.git_sha} clean; manifest {prov.manifest_sha256}; "
        f"digests {prov.model_digests}; written {path}"
    )


@bench_app.command("run")
def bench_run(
    config: Annotated[Path, typer.Option("--config", help="Bench config YAML")],
    final: Annotated[
        bool, typer.Option("--final", help="Test split; needs a host `bench preflight`")
    ] = False,
) -> None:
    """Run (or resume) a bench config. Results: data/bench/<name>/results.jsonl + summary.json."""
    from bourse.bench.live import adversary_runner, pii_runner, session_factory
    from bourse.stack.compose_guard import refuse_if_compose_host

    refuse_if_compose_host(f"bench run --config {config}{' --final' if final else ''}")
    settings = get_settings()
    cfg = load_config(config)
    if final and cfg.split != "test":
        _fail("--final runs the test split only")
    if not final and cfg.split == "test":
        _fail("the test split is for --final runs only; tune on dev")
    run_dir = BENCH_DIR / cfg.name
    env = ollama_env(settings.ollama_base_url, settings.ollama_num_parallel)
    _refuse_on_preconditions(cfg, settings, "bench run")

    async def go() -> list[str]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as http:
            guard = None
            if final:
                guard, prov = await final_guard_from_preflight(
                    cfg,
                    run_dir,
                    roster=settings.roster_models,
                    fetch_ps=lambda: _fetch_ps(http, settings),
                )
                write_lock(run_dir, prov)
            else:
                try:
                    digests = resident_digests(await _fetch_ps(http, settings))
                except (httpx.HTTPError, ValueError) as exc:
                    typer.echo(f"model digests unavailable ({type(exc).__name__})")
                    digests = {}
                prov = dev_provenance(cfg, manifest_sha256=None, env=env, digests=digests)
            typer.echo(
                "model digests (pin these in model_digests for --final): "
                + json.dumps(prov.model_digests)
            )
            result = await run_bench(
                cfg,
                sessions=session_factory(settings, typer.echo),
                adversaries=adversary_runner(settings, typer.echo),
                pii=pii_runner(settings, http),
                guard=guard,
                echo=typer.echo,
            )
            prov = prov.model_copy(update={"manifest_sha256": result.manifest_sha256})
            (result.run_dir / "provenance.json").write_text(prov.model_dump_json(indent=2) + "\n")
            summary = summarize(cfg, result.records, prov, host.load(result.run_dir))
            (result.run_dir / "summary.json").write_text(summary.model_dump_json(indent=2) + "\n")
            typer.echo(
                f"cells run this invocation {result.ran}; finished before (resume) {result.skipped}"
            )
            return format_summary(summary) + _all_error_line(result.records, result.ran)

    try:
        lines = asyncio.run(go())
    except (FinalRefused, BenchRefused, SplitError) as exc:
        _fail(str(exc))
        return
    for line in lines:
        typer.echo(line)
    typer.echo(f"written {run_dir}/summary.json")
    if lines and lines[-1].startswith("BENCH RUN FAILED"):
        raise typer.Exit(3)


@bench_app.command("launch")
def bench_launch(
    config: Annotated[Path, typer.Option("--config", help="Bench config YAML (repo-relative)")],
    final: Annotated[bool, typer.Option("--final", help="Pass --final to bench run")] = False,
    interval: Annotated[
        float, typer.Option("--interval", help="Seconds between host samples", min=5.0)
    ] = 60.0,
) -> None:
    """Host side: start `bench run` in the broker container with stdin from /dev/null, its log
    in data/logs/bench-<name>.log, and sample host conditions (pmset, load) into
    data/bench/<name>/host.jsonl at start, every --interval s, and at the end."""
    import subprocess

    from bourse.stack.compose_guard import in_compose_network

    if in_compose_network():
        _fail("bench launch runs on the host (it reads pmset and starts docker compose)")
    cfg = load_config(config)
    run_dir = BENCH_DIR / cfg.name
    log_path = Path("data/logs") / f"bench-{cfg.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_on_preconditions(cfg, get_settings(), "bench launch")
    argv = ["docker", "compose", "run", "--rm", "-T", "broker", "bourse", "bench", "run"]
    argv += ["--config", config.as_posix()] + (["--final"] if final else [])
    host.append(run_dir, host.sample("start"))
    typer.echo(f"launch {' '.join(argv)} < /dev/null >> {log_path}; host samples {run_dir}/"
               f"{host.HOST_FILE} every {interval:.0f}s")  # fmt: skip
    with log_path.open("a") as log:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT
        )
        while True:
            try:
                code = proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                host.append(run_dir, host.sample("during"))
    host.append(run_dir, host.sample("end"))
    typer.echo(host.format_host(host.summarize_host(host.load(run_dir))))
    if code != 0:
        tail = log_path.read_text().splitlines()[-20:]
        typer.echo(
            f"BENCH RUN FAILED: exit {code}; last {len(tail)} lines of {log_path}:", err=True
        )
        for line in tail:
            typer.echo(f"  | {line}", err=True)
    else:
        typer.echo(f"bench run exited 0; log {log_path}")
    raise typer.Exit(code)


def _summary_for(run_dir: Path) -> Any:
    plan = json.loads((run_dir / "plan.json").read_text())
    cfg = BenchConfig.model_validate(plan["config"])
    prov_path = run_dir / "provenance.json"
    if prov_path.exists():
        prov = Provenance.model_validate_json(prov_path.read_text())
    else:
        prov = dev_provenance(cfg, manifest_sha256=plan.get("manifest_sha256"), env={}, digests={})
    return summarize(cfg, load_records(run_dir), prov, host.load(run_dir))


@bench_app.command("report")
def bench_report(
    run_dir: Annotated[Path, typer.Argument(help="data/bench/<name>")],
) -> None:
    """Aggregate results.jsonl (n/N with Wilson 95% CIs, quantiles, speedup)."""
    for line in format_summary(_summary_for(run_dir)):
        typer.echo(line)


@bench_app.command("sizing")
def bench_sizing(
    run_dir: Annotated[Path, typer.Argument(help="data/bench/<pilot name>")],
    hours: Annotated[float, typer.Option(help="Hours per night")] = 8.0,
) -> None:
    """Seconds per case from a dev pilot -> N per config and the Wilson half-width it buys."""
    summary = _summary_for(run_dir)
    if summary.split != "dev":
        _fail("sizing reads a dev pilot only")
    for line in sizing_lines(summary, hours=hours):
        typer.echo(line)
