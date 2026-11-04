"""Public market snapshots for simulated execution. No exchange orders are sent."""

import math
import time
from types import SimpleNamespace


def candle_context(rows, period, side):
    if len(rows) <= period:
        raise ValueError("Insufficient candle history for ATR")
    window = rows[-period - 1:]
    ranges = [max(row[2] - row[3], abs(row[2] - prev[4]), abs(row[3] - prev[4]))
              for prev, row in zip(window, window[1:])]
    atr = sum(ranges) / period
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError("Invalid ATR")
    swing = min(r[3] for r in window[:-1]) if side == "long" else max(r[2] for r in window[:-1])
    regime = (1 + (1 if side == "long" else -1)
              * (1 if window[-1][4] > window[0][4] else -1)) / 2
    return atr, swing, regime


class MarketView:
    def __init__(self, exchange, symbol, side, cfg):
        self.exchange = exchange
        self.symbol = symbol
        self.side = side
        self.cfg = cfg
        self.trades = []

    def now(self):
        return time.monotonic()

    async def refresh(self):
        start = self.now()
        data = self.exchange.market_data
        book = await self.exchange.fetch_order_book(self.symbol, limit=20)
        received_at = self.now()
        self.request_ms = (received_at - start) * 1000
        tick = float(data.market(self.symbol)["precision"]["price"])
        self.bids, self.asks = book["bids"], book["asks"]
        bid, ask = float(self.bids[0][0]), float(self.asks[0][0])
        if not all(math.isfinite(v) and v > 0 for v in (bid, ask, tick)) or ask <= bid:
            raise ValueError("Invalid order book")
        self.book = SimpleNamespace(best_bid=bid, best_ask=ask, tick=tick)
        # Executable-price checks use the same book as spread and depth checks.
        self.price = (bid + ask) / 2
        timestamp = book.get("timestamp")
        if timestamp is not None and math.isfinite(float(timestamp)):
            self._latency = abs(time.time() * 1000 - float(timestamp))
            self.latency_source = "exchange_book_timestamp"
        else:
            # Without a source timestamp, include the entire request duration.
            self._latency = self.request_ms
            self.latency_source = "request_duration"
        self._received_at = received_at

    async def refresh_flow(self):
        try:
            since = int((time.time() - self.cfg["flow_window_sec"]) * 1000)
            self.trades = await self.exchange.market_data.fetch_trades(self.symbol, since=since, limit=1000)
        except (AttributeError, NotImplementedError):
            self.trades = []
        except Exception:
            self.trades = []

    def flow(self):
        cutoff = (time.time() - self.cfg["flow_window_sec"]) * 1000
        return [t for t in self.trades if (t.get("timestamp") or 0) >= cutoff
                and t.get("side") in ("buy", "sell")]

    def cvd_30s(self):
        return sum(float(t["amount"]) * (1 if t["side"] == "buy" else -1) for t in self.flow())

    def detect_absorption(self, side):
        trades = sorted(self.flow(), key=lambda trade: trade["timestamp"])
        if len(trades) < 2:
            return False
        direction = 1 if side == "long" else -1
        return (direction * self.cvd_30s() > 0
                and direction * (float(trades[-1]["price"]) - float(trades[0]["price"])) <= 0)

    def spread(self):
        return (self.book.best_ask - self.book.best_bid) / ((self.book.best_bid + self.book.best_ask) / 2)

    def latency_ms(self):
        return self._latency + max(0.0, self.now() - self._received_at) * 1000

    def sweep(self, qty, bound=None):
        direction = 1 if self.side == "long" else -1
        levels = self.asks if direction == 1 else self.bids
        remaining, cost = qty, 0.0
        for raw_price, raw_amount, *_ in levels:
            price, available = float(raw_price), float(raw_amount)
            if not math.isfinite(price) or not math.isfinite(available) or price <= 0 or available <= 0:
                continue
            if bound is not None and direction * (price - bound) > 0:
                break
            take = min(remaining, available)
            cost += take * price
            remaining -= take
            if remaining <= 0:
                return cost / qty
        return None

    def expected_slippage(self, qty):
        average = self.sweep(qty)
        touch = self.book.best_ask if self.side == "long" else self.book.best_bid
        return float("inf") if average is None else abs(average / touch - 1) * 10000
