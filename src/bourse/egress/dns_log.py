"""dnsmasq query-log parser and watcher (ADR-0035 amendment, Phase 5b).

A line `query[A] name from ip` from an agent IP for a name that is not on the topology
allowlist is a HARD DNS_EGRESS violation. The query is answered by the sinkhole; nothing
leaves bourse_internal.
"""

import hashlib
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio

from bourse.contracts.trust import EgressEvent, Severity, ViolationReport
from bourse.trust.repo import EgressRepository

DNS_EGRESS = "DNS_EGRESS"
QUERY = re.compile(r"query\[(?P<qtype>[A-Za-z0-9]+)\]\s+(?P<name>\S+)\s+from\s+(?P<src>\S+)")
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class DnsQuery:
    qtype: str
    name: str
    src_ip: str


def parse_query_line(line: str) -> DnsQuery | None:
    match = QUERY.search(line)
    if match is None:
        return None
    return DnsQuery(match.group("qtype"), match.group("name").rstrip("."), match.group("src"))


def is_allowlisted(name: str, allow: frozenset[str]) -> bool:
    host = name.lower().rstrip(".")
    if host in allow:
        return True
    return any(host.endswith("." + allowed) for allowed in allow)


class DnsLogWatcher:
    def __init__(
        self,
        *,
        identities: Mapping[str, str],
        agent_ips: Mapping[str, str],
        allow: frozenset[str],
        repo: EgressRepository,
        report: Callable[[str, ViolationReport], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.identities = identities
        self.agent_ips = agent_ips
        self.allow = frozenset(n.lower().rstrip(".") for n in allow)
        self.repo = repo
        self.report = report
        self.clock = clock

    async def handle_line(self, line: str) -> EgressEvent | None:
        query = parse_query_line(line)
        if query is None or is_allowlisted(query.name, self.allow):
            return None
        agent_id = self.agent_ips.get(query.src_ip)
        event = EgressEvent(
            src_ip=query.src_ip,
            agent_id=self.identities.get(query.src_ip),
            host=query.name,
            port=53,
            bytes=0,
            payload_sha256=EMPTY_SHA,
            honeytoken_hits=0,
            at=self.clock(),
        )
        await self.repo.add_event(event)
        if agent_id is not None:
            await self.report(
                agent_id,
                ViolationReport(
                    kind=DNS_EGRESS,
                    severity=Severity.HARD,
                    evidence=(
                        f"DNS {query.qtype} {query.name} from {query.src_ip}; source=dnsmasq"
                    ),
                ),
            )
        return event

    async def follow(self, path: Path) -> None:
        """Tail a dnsmasq --log-facility file. Starts at the current end."""
        apath = anyio.Path(path)
        await apath.parent.mkdir(parents=True, exist_ok=True)
        await apath.touch()
        async with await anyio.open_file(path) as fh:
            await fh.seek(0, 2)
            while True:
                line = await fh.readline()
                if line:
                    await self.handle_line(line)
                else:
                    await anyio.sleep(0.2)


def _skip_name(data: bytes, offset: int) -> int:
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
    raise ValueError("truncated DNS name")


def parse_a_answers(data: bytes) -> list[str]:
    """IPv4 addresses from a DNS response. Empty on rcode != 0 or truncated packet."""
    if len(data) < 12 or (data[3] & 0x0F) != 0:
        return []
    qd = int.from_bytes(data[4:6], "big")
    an = int.from_bytes(data[6:8], "big")
    offset = 12
    try:
        for _ in range(qd):
            offset = _skip_name(data, offset) + 4
        ips: list[str] = []
        for _ in range(an):
            offset = _skip_name(data, offset)
            rtype = int.from_bytes(data[offset : offset + 2], "big")
            rdlen = int.from_bytes(data[offset + 8 : offset + 10], "big")
            rdata = data[offset + 10 : offset + 10 + rdlen]
            offset = offset + 10 + rdlen
            if rtype == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in rdata))
        return ips
    except (ValueError, IndexError):
        return []


def dns_query_a(name: str, server: str = "127.0.0.1", timeout: float = 2.0) -> list[str]:
    """UDP DNS A query against `server`:53 (compose healthcheck / A6c)."""
    labels = name.rstrip(".").encode().split(b".")
    qname = b"".join(bytes([len(part)]) + part for part in labels) + b"\x00"
    req = b"\xaa\xbb\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(req, (server, 53))
        data, _ = sock.recvfrom(512)
    except OSError:
        return []
    finally:
        sock.close()
    return parse_a_answers(data)


def dns_health_ok(server: str = "127.0.0.1") -> str | None:
    """None if allowlisted `registry` resolves off-sinkhole and `attacker.example` is sinkholed."""
    from bourse.stack.topology import EGRESS_DNS_IP, SINKHOLE_IP

    def _check(host: str) -> str | None:
        registry = dns_query_a("registry", server=host)
        if not registry:
            return f"registry did not resolve via {host}:53"
        if SINKHOLE_IP in registry:
            return f"registry sinkholed ({registry})"
        attacker = dns_query_a("attacker.example", server=host)
        if attacker != [SINKHOLE_IP]:
            return f"attacker.example expected [{SINKHOLE_IP}] got {attacker}"
        return None

    problem = _check(server)
    if problem is None:
        return None
    if server == "127.0.0.1":
        fallback = _check(EGRESS_DNS_IP)
        if fallback is None:
            return None
        return f"{problem}; {fallback}"
    return problem
