import asyncio
from types import SimpleNamespace

import pytest

from execution.adaptive_runtime import CONFIG
from execution.entry_engine import execution_guard
from execution.market_view import MarketView


def view(request_ms, timestamp=None):
    clock = [0.0]

    async def fetch_book(*args, **kwargs):
        clock[0] += request_ms / 1000
        book = {"bids": [[99.99, 1000]], "asks": [[100.01, 1000]]}
        if timestamp is not None:
            book["timestamp"] = timestamp
        return book

    data = SimpleNamespace(market=lambda symbol: {"precision": {"price": .01}})
    exchange = SimpleNamespace(market_data=data, fetch_order_book=fetch_book)
    market = MarketView(exchange, "BTC", "long", CONFIG["adaptive"])
    market.now = lambda: clock[0]
    return market, clock


def test_mobile_rest_request_above_200ms_can_pass_without_ticker_request():
    market, _ = view(350)
    asyncio.run(market.refresh())
    order = {"qty": 1, "ref_price": 100}
    assert market.price == 100
    assert market.request_ms == pytest.approx(350)
    assert execution_guard(order, market, {**CONFIG["execution"], "max_latency_ms": 200}) == "latency_spike"
    assert execution_guard(order, market, CONFIG["execution"]) is None


def test_slow_or_aged_book_is_still_rejected():
    market, clock = view(2200)
    asyncio.run(market.refresh())
    assert execution_guard({"qty": 1, "ref_price": 100}, market, CONFIG["execution"]) == "latency_spike"
    market, clock = view(100)
    asyncio.run(market.refresh())
    clock[0] += 2.1
    assert market.latency_ms() > CONFIG["execution"]["max_latency_ms"]


def test_exchange_timestamp_reports_data_age_instead_of_queue_wait(monkeypatch):
    monkeypatch.setattr("execution.market_view.time.time", lambda: 1000.0)
    market, _ = view(1500, timestamp=999950)
    asyncio.run(market.refresh())
    assert market.latency_source == "exchange_book_timestamp"
    assert market.request_ms == pytest.approx(1500)
    assert market.latency_ms() == pytest.approx(50)
    assert execution_guard({"qty": 1, "ref_price": 100}, market, CONFIG["execution"]) is None


def test_stale_timestamp_does_not_pass_with_fast_request(monkeypatch):
    monkeypatch.setattr("execution.market_view.time.time", lambda: 1000.0)
    market, _ = view(10, timestamp=997000)
    asyncio.run(market.refresh())
    assert execution_guard({"qty": 1, "ref_price": 100}, market, CONFIG["execution"]) == "latency_spike"
