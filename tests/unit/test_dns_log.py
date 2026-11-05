from datetime import UTC, datetime

import pytest

from bourse.contracts.trust import ViolationReport
from bourse.egress import dns_log
from bourse.egress.dns_log import DnsLogWatcher, parse_a_answers, parse_query_line
from bourse.stack.topology import SINKHOLE_IP
from bourse.trust.repo import InMemoryEgressRepository


def test_parse_query_line() -> None:
    q = parse_query_line("dnsmasq[1]: query[A] abcd1234.attacker.example from 172.30.55.80")
    assert q is not None
    assert q.qtype == "A" and q.name == "abcd1234.attacker.example"
    assert q.src_ip == "172.30.55.80"
    assert parse_query_line("forwarded example.com to 127.0.0.11") is None


def test_parse_a_answers() -> None:
    qname = b"\x08registry\x00\x00\x01\x00\x01"
    header = b"\xaa\xbb\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x00\x00\x04\xac\x1e\x37\x14"
    assert parse_a_answers(header + qname + answer) == ["172.30.55.20"]
    assert parse_a_answers(b"short") == []


async def test_watcher_reports_dns_egress_for_agent() -> None:
    reported: list[tuple[str, str]] = []

    async def report(agent_id: str, v: ViolationReport) -> None:
        reported.append((agent_id, v.kind))

    watcher = DnsLogWatcher(
        identities={"172.30.55.80": "exfil-dns@vendor-x.local"},
        agent_ips={"172.30.55.80": "exfil-dns@vendor-x.local"},
        allow=frozenset({"meter", "registry", "postgres"}),
        repo=InMemoryEgressRepository(),
        report=report,
        clock=lambda: datetime(2026, 11, 1, tzinfo=UTC),
    )
    event = await watcher.handle_line("query[A] deadbeef.attacker.example from 172.30.55.80")
    assert event is not None and event.port == 53
    assert reported == [("exfil-dns@vendor-x.local", "DNS_EGRESS")]
    skipped = await watcher.handle_line("query[A] meter from 172.30.55.80")
    assert skipped is None


def test_dns_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_query(name: str, server: str = "127.0.0.1", **_: object) -> list[str]:
        if name == "registry":
            return ["172.30.55.20"]
        if name == "attacker.example":
            return [SINKHOLE_IP]
        return []

    monkeypatch.setattr(dns_log, "dns_query_a", fake_query)
    assert dns_log.dns_health_ok() is None
