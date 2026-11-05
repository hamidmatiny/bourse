"""docker-compose.yml, egress DNS config and the IP -> identity map come from one list."""

from pathlib import Path
from typing import Any

import yaml

from bourse.agents.catalog import agent_specs
from bourse.registry.service import make_agent_id
from bourse.stack import topology

ROOT = Path(__file__).resolve().parents[2]


def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return data


def test_committed_compose_file_is_generated() -> None:
    assert compose() == topology.compose_document()


def test_agents_internal_only_no_ports_own_key_only() -> None:
    services = compose()["services"]
    agents = {n.name: n for n in topology.agent_nodes()}
    assert len(agents) == 5
    for name, node in agents.items():
        svc = services[name]
        assert set(svc["networks"]) == {topology.INTERNAL}
        assert "ports" not in svc
        assert svc["volumes"] == [f"./data/keys/{node.vendor}:/app/data/keys/{node.vendor}:ro"]
        assert svc["dns"] == [topology.EGRESS_DNS_IP]
        assert svc["networks"][topology.INTERNAL]["aliases"] == [node.aliases[0]]
    networks = compose()["networks"]
    assert networks[topology.INTERNAL]["internal"] is True


def test_unexpected_exit_is_terminal_except_migrate() -> None:
    from bourse.cli import _unexpected_exit

    assert _unexpected_exit({"Service": "egress-dns", "State": "exited"})
    assert _unexpected_exit({"Service": "egress-dns", "State": "running", "Health": "unhealthy"})
    assert not _unexpected_exit({"Service": "migrate", "State": "exited"})
    assert not _unexpected_exit({"Service": "registry", "State": "running"})
    assert not _unexpected_exit({"Service": "registry", "State": "running", "Health": "starting"})


def test_postgres_publishes_only_on_localhost() -> None:
    svc = compose()["services"]["postgres"]
    assert svc["ports"] == ["127.0.0.1:55432:5432"]
    assert topology.HOST_NET in svc["networks"]
    assert topology.INTERNAL in svc["networks"]


def test_egress_dns_command_is_valid_shell() -> None:
    cmd = compose()["services"]["egress-dns"]["command"]
    script = cmd[-1] if isinstance(cmd, list) else cmd
    assert "& &&" not in script
    assert "log-queries" in script
    assert "dns-watch" in script
    # dns-watch must be backgrounded only AFTER conf is written (brace group).
    assert "{ bourse egress dns-watch" in script
    assert script.strip().endswith("; }")
    assert "--conf-file=/tmp/dnsmasq.conf" in script
    health = compose()["services"]["egress-dns"]["healthcheck"]["test"]
    assert health[:3] == ["CMD", "python", "-c"]
    assert "dns_health_ok" in health[3]


def test_only_meter_registry_and_tools_reach_the_host_network() -> None:
    services = compose()["services"]
    on_host = sorted(n for n, s in services.items() if topology.HOST_NET in s.get("networks", {}))
    assert on_host == ["jaeger", "meter", "otel-collector", "postgres", "registry"]
    assert services["meter"]["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert "ports" not in services["meter"]


def test_vendor_hostnames() -> None:
    hosts = sorted(a for n in topology.agent_nodes() for a in n.aliases)
    assert hosts == [
        "normalizer.vendor-b.local",
        "pii-fast.vendor-c.local",
        "pii-precise.vendor-b.local",
        "profiler.vendor-a.local",
        "reporter.vendor-a.local",
    ]


def test_ip_identity_map_matches_catalog_agent_ids() -> None:
    ips = [n.ip for n in topology.nodes()]
    assert len(ips) == len(set(ips))
    expected = {
        make_agent_id(s.display_name, s.vendor_domain) for s in agent_specs("l", "s").values()
    }
    identities = set(topology.agent_ids_by_ip().values())
    assert expected <= identities
    assert topology.ip_identities()[topology.PREFIX + "70"] == "egress-probe"
    from bourse.adversaries.catalog import VENDOR, adversary_specs

    adv = {make_agent_id(s.display_name, VENDOR) for s in adversary_specs("s").values()}
    assert adv <= identities


def test_dnsmasq_config_from_the_same_list() -> None:
    conf = topology.dnsmasq_conf()
    for name in topology.allowlisted_names():
        assert f"server=/{name}/127.0.0.11" in conf
    assert f"address=/#/{topology.SINKHOLE_IP}" in conf
    assert f"listen-address={topology.EGRESS_DNS_IP}" in conf
    assert "listen-address=127.0.0.1" in conf
    assert "attacker.example" not in conf
    names = set(topology.allowlisted_names())
    assert {"meter", "registry", "postgres", "profiler.vendor-a.local"} <= names


def test_compose_down_includes_every_profile_from_file() -> None:
    """stack down must pass --profile for every profile declared in compose (not hard-coded)."""
    from bourse.stack.launcher import compose_profile_args, compose_profiles

    declared: set[str] = set()
    for svc in topology.compose_document()["services"].values():
        declared.update(svc.get("profiles") or [])
    assert declared and set(compose_profiles()) == declared
    args = compose_profile_args() + ["down", "-v", "--remove-orphans"]
    assert args[-3:] == ["down", "-v", "--remove-orphans"]
    flagged = {args[i + 1] for i, a in enumerate(args[:-3]) if a == "--profile"}
    assert flagged == declared


def test_env_files_give_each_agent_only_its_key() -> None:
    from bourse.adversaries.catalog import ADVERSARY_NAMES

    names = [*agent_specs("l", "s"), "broker", *ADVERSARY_NAMES]
    keys = {name: f"key-{name}-0123456789abcdef" for name in names}
    files = topology.env_files(
        {"meter_keys": keys, "admin_token": "admin-0123456789", "honeytoken_secret": "h" * 32},
        model_large="L",
        model_small="S",
    )
    for node in topology.agent_nodes():
        env = files[node.name]
        assert f"BOURSE_AGENT_KEY={keys[node.agent or '']}\n" in env
        others = [k for name, k in keys.items() if name != node.agent]
        assert not any(k in env for k in others)
        assert "BOURSE_ADMIN_TOKEN" not in env and "BOURSE_DEV_HOSTS=\n" in env
    assert "BOURSE_DEV_HOSTS=\n" in files["registry"]
