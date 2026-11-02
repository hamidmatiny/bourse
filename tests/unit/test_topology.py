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
    assert set(topology.agent_ids_by_ip().values()) == expected
    assert topology.ip_identities()[topology.PREFIX + "70"] == "egress-probe"


def test_dnsmasq_config_from_the_same_list() -> None:
    conf = topology.dnsmasq_conf()
    for name in topology.allowlisted_names():
        assert f"server=/{name}/127.0.0.11" in conf
    assert f"address=/#/{topology.SINKHOLE_IP}" in conf
    assert "attacker.example" not in conf
    names = set(topology.allowlisted_names())
    assert {"meter", "registry", "postgres", "profiler.vendor-a.local"} <= names


def test_env_files_give_each_agent_only_its_key() -> None:
    keys = {name: f"key-{name}-0123456789abcdef" for name in [*agent_specs("l", "s"), "broker"]}
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
