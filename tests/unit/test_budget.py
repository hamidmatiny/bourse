import threading

import pytest
import respx

from bourse.contracts.metering import Usage
from bourse.llm.base import ChatMessage
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetExceeded, BudgetLedger
from tests.unit.fakes import OLLAMA_CHAT, make_router, make_spec, ollama_reply, request_json


def _usage(tokens_in: int, tokens_out: int) -> Usage:
    return Usage(tokens_in=tokens_in, tokens_out=tokens_out, wall_ms=1.0, model="m", provider="p")


def test_charge_past_run_budget_raises() -> None:
    ledger = BudgetLedger(make_spec(max_tokens_run=100))
    ledger.charge(_usage(40, 40))
    with pytest.raises(BudgetExceeded):
        ledger.charge(_usage(15, 15))
    assert ledger.tokens_used == 110
    assert len(ledger.entries) == 2
    with pytest.raises(BudgetExceeded):
        ledger.cap_call(10)


def test_charge_is_thread_safe() -> None:
    ledger = BudgetLedger(make_spec(max_tokens_run=10_000_000))

    def work() -> None:
        for _ in range(1000):
            ledger.charge(_usage(1, 1))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.tokens_used == 8 * 1000 * 2
    assert [e.seq for e in ledger.entries] == list(range(8000))


async def test_per_call_max_tokens_is_passed_to_provider() -> None:
    mock = respx.Router(assert_all_called=False)
    route = mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("hi"))
    router = make_router(mock, CircuitBreaker(), make_spec(max_tokens_per_call=64))
    await router.chat([ChatMessage(role="user", content="x")], [], 1000, 5.0)
    assert request_json(route.calls.last.request)["options"]["num_predict"] == 64
    await router.chat([ChatMessage(role="user", content="x")], [], 32, 5.0)
    assert request_json(route.calls.last.request)["options"]["num_predict"] == 32


async def test_router_charges_ledger() -> None:
    mock = respx.Router(assert_all_called=False)
    mock.post(OLLAMA_CHAT).mock(return_value=ollama_reply("hi", prompt_tokens=11, out_tokens=4))
    router = make_router(mock, CircuitBreaker())
    await router.chat([ChatMessage(role="user", content="x")], [], 16, 5.0)
    assert router.ledger.tokens_used == 15
