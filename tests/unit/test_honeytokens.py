import json

from bourse.mcp_servers.pii import classify, luhn_valid
from bourse.trust.honeytokens import derive, hits

SECRET = b"test-secret-not-a-real-key"


def test_luhn_valid_and_classified() -> None:
    for i in range(200):
        t = derive(SECRET, f"run-x-{i}:pii:1")
        assert len(t.card) == 16 and luhn_valid(t.card)
        assert classify(t.card) == "CARD" and classify(t.email) == "EMAIL"


def test_unique_and_deterministic_per_task_id() -> None:
    tokens = [derive(SECRET, f"run-{i}:stats:1") for i in range(1000)]
    assert len({t.card for t in tokens}) == 1000
    assert len({t.email for t in tokens}) == 1000
    assert derive(SECRET, "run-1:stats:1") == tokens[1]
    assert derive(b"other-secret", "run-1:stats:1") != tokens[1]


def test_matching_in_payloads() -> None:
    t = derive(SECRET, "run-9:pii:1")
    grouped = "-".join(t.card[i : i + 4] for i in range(0, 16, 4))
    assert hits(f"GET /?c={t.card} HTTP/1.1".encode(), [t]) == 1
    assert hits(json.dumps({"card": grouped}).encode(), [t]) == 1
    assert hits(f"POST /x HTTP/1.1\r\n\r\nmail={t.email.upper()}".encode(), [t]) == 1
    other = derive(SECRET, "run-10:pii:1")
    assert hits(f"c={other.card}".encode(), [t]) == 0
    assert hits(b"", [t]) == 0
