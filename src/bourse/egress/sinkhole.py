"""Egress sinkhole (ADR-0035).

egress-dns resolves every non-allowlisted name to this container. It listens on 80 and 443,
reads at most 4 KiB, answers HTTP with 403 and closes TLS without a handshake. Each connection
becomes one append-only egress_event: src_ip, identity from the static IP map, Host header or
TLS SNI, port, bytes read, sha256 of the bytes, honeytoken hits. Events from an agent IP are
reported to the registry as HARD EGRESS_ATTEMPT, plus HARD EXFIL when a honeytoken appears in
the payload. The evidence is observed on the network at a static IP, so it is attributed.
"""

import contextlib
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
from anyio.abc import SocketAttribute, SocketStream

from bourse.contracts.trust import EgressEvent, Severity, ViolationReport
from bourse.trust.honeytokens import hits
from bourse.trust.repo import EgressRepository, TokenStore

MAX_BYTES = 4 * 1024  # first 4 KiB of each connection
READ_DEADLINE_S = 5.0
FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: 22\r\n"
    b"Connection: close\r\n\r\negress is not allowed\n"
)
EGRESS_ATTEMPT = "EGRESS_ATTEMPT"
EXFIL = "EXFIL"


@dataclass(frozen=True)
class HttpHead:
    method: str
    path: str
    host: str | None


def parse_http(data: bytes) -> HttpHead | None:
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1", errors="replace")
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3 or not parts[2].startswith("HTTP/"):
        return None
    host = None
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "host":
            host = value.strip().split(":")[0] or None
    return HttpHead(method=parts[0], path=parts[1], host=host)


def parse_sni(data: bytes) -> str | None:
    """server_name from a TLS ClientHello; None if the bytes are not one or carry no SNI."""
    try:
        if len(data) < 5 or data[0] != 0x16:  # handshake record
            return None
        pos = 5
        if data[pos] != 0x01:  # ClientHello
            return None
        pos += 4  # type + 3-byte length
        pos += 2 + 32  # client_version + random
        pos += 1 + data[pos]  # session_id
        pos += 2 + int.from_bytes(data[pos : pos + 2], "big")  # cipher_suites
        pos += 1 + data[pos]  # compression_methods
        end = pos + 2 + int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
        while pos + 4 <= min(end, len(data)):
            ext_type = int.from_bytes(data[pos : pos + 2], "big")
            ext_len = int.from_bytes(data[pos + 2 : pos + 4], "big")
            body = data[pos + 4 : pos + 4 + ext_len]
            if ext_type == 0 and len(body) >= 5:  # server_name
                name_len = int.from_bytes(body[3:5], "big")
                if body[2] == 0:  # host_name
                    return body[5 : 5 + name_len].decode("ascii", errors="replace")
            pos += 4 + ext_len
    except IndexError:
        return None
    return None


Reporter = Callable[[str, ViolationReport], Awaitable[None]]


class Sinkhole:
    def __init__(
        self,
        *,
        identities: Mapping[str, str],
        agent_ips: Mapping[str, str],
        repo: EgressRepository,
        tokens: TokenStore,
        report: Reporter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.identities = identities
        self.agent_ips = agent_ips
        self.repo = repo
        self.tokens = tokens
        self.report = report
        self.clock = clock

    async def record(self, src_ip: str, port: int, data: bytes) -> EgressEvent:
        host: str | None
        if port == 443 or (data[:1] == b"\x16"):
            host = parse_sni(data)
        else:
            head = parse_http(data)
            host = head.host if head else None
        n_hits = hits(data, await self.tokens.list_tokens()) if data else 0
        event = EgressEvent(
            src_ip=src_ip,
            agent_id=self.identities.get(src_ip),
            host=host,
            port=port,
            bytes=len(data),
            payload_sha256=hashlib.sha256(data).hexdigest(),
            honeytoken_hits=n_hits,
            at=self.clock(),
        )
        await self.repo.add_event(event)
        agent_id = self.agent_ips.get(src_ip)
        if agent_id is not None:
            evidence = (
                f"egress to {host or '?'}:{port} from {src_ip}; {len(data)} bytes; "
                f"sha256={event.payload_sha256}; source=network"
            )
            await self.report(
                agent_id,
                ViolationReport(kind=EGRESS_ATTEMPT, severity=Severity.HARD, evidence=evidence),
            )
            if n_hits:
                await self.report(
                    agent_id,
                    ViolationReport(
                        kind=EXFIL,
                        severity=Severity.HARD,
                        evidence=f"{n_hits} honeytoken(s) in payload; {evidence}",
                    ),
                )
        return event

    async def handle(self, stream: SocketStream, port: int) -> None:
        async with stream:
            src_ip = str(stream.extra(SocketAttribute.remote_address)[0])
            data = b""
            with anyio.move_on_after(READ_DEADLINE_S):
                while len(data) < MAX_BYTES:
                    try:
                        chunk = await stream.receive(MAX_BYTES - len(data))
                    except (anyio.EndOfStream, anyio.BrokenResourceError):
                        break
                    data += chunk
                    if port != 443 and b"\r\n\r\n" in data:
                        break
                    if port == 443 and len(data) >= 5:
                        record_len = 5 + int.from_bytes(data[3:5], "big")
                        if len(data) >= record_len:
                            break
            await self.record(src_ip, port, data[:MAX_BYTES])
            if port != 443:
                with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                    await stream.send(FORBIDDEN)

    async def serve(self, ports: list[int], host: str = "0.0.0.0") -> None:
        async with anyio.create_task_group() as tg:
            for port in ports:
                listener = await anyio.create_tcp_listener(local_host=host, local_port=port)

                async def handler(stream: SocketStream, p: int = port) -> None:
                    await self.handle(stream, p)

                tg.start_soon(listener.serve, handler)
