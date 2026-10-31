"""Local stack: 3 MCP servers, meter, registry, 5 agents as subprocesses (ADR-0021).

Logs: data/logs/<name>.log. PID files: data/run/<name>.pid. Per-run secrets (agent meter keys,
registry admin token) are generated into data/run/secrets.json (mode 0600, gitignored).
"""

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bourse.agents.catalog import AGENT_NAMES, specs_from_settings
from bourse.agents.pii_precise.isolation import CREWAI_ENV
from bourse.config import Settings
from bourse.mcp_servers.common import SERVER_PORTS

RUN_DIR = Path("data/run")
LOG_DIR = Path("data/logs")
AGENT_HOME_DIR = Path("data/agent_home")
SECRETS_FILE = RUN_DIR / "secrets.json"
METER_PORT = 55200
REGISTRY_PORT = 55300
HEALTH_DEADLINE_S = 120.0
BROKER_METER_ID = "broker"


@dataclass(frozen=True)
class Service:
    name: str
    argv: list[str]
    port: int
    health_path: str
    env: dict[str, str] = field(default_factory=dict)


METER_CLIENTS = (*AGENT_NAMES, BROKER_METER_ID)


def load_secrets() -> dict[str, Any]:
    """Meter keys per client and the registry admin token; missing entries are added."""
    data: dict[str, Any] = {}
    if SECRETS_FILE.exists():
        data = json.loads(SECRETS_FILE.read_text())
    keys: dict[str, str] = data.setdefault("meter_keys", {})
    missing = [name for name in METER_CLIENTS if name not in keys]
    if not missing and "admin_token" in data:
        return data
    for name in missing:
        keys[name] = secrets.token_hex(24)
    data.setdefault("admin_token", secrets.token_hex(24))
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    return data


def meter_key(name: str) -> str:
    return str(load_secrets()["meter_keys"][name])


def admin_token(settings: Settings) -> str:
    return settings.bourse_admin_token.get_secret_value() or str(load_secrets()["admin_token"])


def services(settings: Settings) -> list[Service]:
    sec = load_secrets()
    cli = [sys.executable, "-m", "bourse.cli"]
    meter_keys = ",".join(f"{a}:{k}" for a, k in sec["meter_keys"].items())
    existing = settings.bourse_meter_keys.get_secret_value()
    all_keys = ",".join(filter(None, [existing, meter_keys]))
    # MCP servers accept the same per-agent keys (X-Bourse-Agent-Key) and record tool_usage.
    out = [
        Service(
            f"mcp-{n}",
            [*cli, "mcp", "serve", n, "--port", str(p)],
            p,
            "/mcp",
            {"BOURSE_METER_KEYS": all_keys},
        )
        for n, p in SERVER_PORTS.items()
    ]
    out.append(
        Service(
            "meter",
            [*cli, "meter", "serve", "--port", str(METER_PORT)],
            METER_PORT,
            "/v1/chat/completions",
            {"BOURSE_METER_KEYS": all_keys},
        )
    )
    out.append(
        Service(
            "registry",
            [*cli, "registry", "serve", "--port", str(REGISTRY_PORT)],
            REGISTRY_PORT,
            "/agents",
            {
                "BOURSE_ADMIN_TOKEN": admin_token(settings),
                "BOURSE_DEV_HOSTS": settings.bourse_dev_hosts or "127.0.0.1,localhost",
            },
        )
    )
    for name, spec in specs_from_settings(settings).items():
        env = {
            "BOURSE_AGENT_KEY": str(sec["meter_keys"][name]),
            "BOURSE_METER_URL": f"http://127.0.0.1:{METER_PORT}",
        }
        if spec.framework == "crewai":
            home = (AGENT_HOME_DIR / name).resolve()
            home.mkdir(parents=True, exist_ok=True)
            env |= CREWAI_ENV | {"HOME": str(home)}
        out.append(
            Service(
                f"agent-{name}",
                [*cli, "agent", "serve", name],
                spec.port,
                "/.well-known/agent-card.json",
                env,
            )
        )
    return out


def _pid_file(name: str) -> Path:
    return RUN_DIR / f"{name}.pid"


def read_pid(name: str) -> int | None:
    path = _pid_file(name)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return None
    return pid


def healthy(svc: Service) -> bool:
    # Status line only: GET /mcp opens a server-sent-event stream that never finishes.
    url = f"http://127.0.0.1:{svc.port}{svc.health_path}"
    try:
        with httpx.stream("GET", url, timeout=2.0) as resp:
            return resp.status_code < 500
    except httpx.HTTPError:
        return False


def start(svc: Service) -> int:
    existing = read_pid(svc.name)
    if existing is not None:
        return existing
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / f"{svc.name}.log").open("ab")
    proc = subprocess.Popen(
        svc.argv,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, **svc.env},
        start_new_session=True,
    )
    _pid_file(svc.name).write_text(f"{proc.pid}\n")
    return proc.pid


def wait_healthy(svcs: list[Service], deadline_s: float = HEALTH_DEADLINE_S) -> dict[str, bool]:
    status = {s.name: False for s in svcs}
    end = time.monotonic() + deadline_s
    while time.monotonic() < end and not all(status.values()):
        for s in svcs:
            if not status[s.name]:
                status[s.name] = healthy(s)
        if not all(status.values()):
            time.sleep(0.5)
    return status


def stop(name: str, grace_s: float = 10.0) -> bool:
    pid = read_pid(name)
    _pid_file(name).unlink(missing_ok=True)
    if pid is None:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    end = time.monotonic() + grace_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    return True


def table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows)
