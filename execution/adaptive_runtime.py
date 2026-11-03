"""Paper-only orchestration for risk-budgeted Cascade entries and exits."""

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from alpha.micro_confirm import await_micro_confirmation, quality_gate
from execution.entry_engine import execution_guard, plan_entry
from execution.market_view import MarketView, candle_context
from risk.exit_engine import ExitEngine, Position
from risk.sizing import position_size_from_risk
from risk.stop_engine import StopEngine, compute_liq_price

log = logging.getLogger("CASCADE")
CONFIG = json.loads((Path(__file__).resolve().parents[1] / "baseline.json").read_text())


class AdaptiveExecution:
    def entry_allowed(self, symbol, now):
        return (
            symbol not in self.positions
            and len(self.positions) < self.strategy.MAX_CONCURRENT_POSITIONS
            and now - self.last_trade.get(symbol, -float("inf")) >= self.strategy.COOLDOWN_SEC
            and now - self.last_loss.get(symbol, -float("inf")) >= self.strategy.SAME_SYMBOL_LOSS_COOLDOWN
        )

    def record_execution(self, signal_id, symbol, reason):
        self.execution_events.append({
            "signal_id": signal_id, "symbol": symbol, "reason": reason, "timestamp": time.time()
        })
        self.save()

    def risk_plan(self, side, price, atr, swing, multiplier, entry_slippage=0.0):
        cfg = CONFIG["risk"]
        direction = 1 if side == "long" else -1
        liq = compute_liq_price(side, price, self.exchange.leverage, cfg["mmr"])
        plan = StopEngine(cfg).compute_initial_stop(side, price, atr, swing, liq)
        equity = self.exchange.equity
        if equity <= 0:
            raise ValueError("Insufficient equity")
        open_risk = sum(
            max(0.0, (p["entry"] - p["sl"]) * (1 if p["side"] == "buy" else -1))
            * p["amount"] + p["entry_fee"] + p["sl"] * p["amount"] * self.exchange.fee_rate
            for p in self.positions.values()
        )
        budget = min(equity * cfg["risk_pct_per_trade"] * multiplier,
                     equity * cfg["max_portfolio_risk_pct"] - open_risk)
        if budget <= 0:
            raise ValueError("Portfolio risk budget exhausted")
        cost_distance = (plan.distance_pct + self.exchange.fee_rate * (1 + plan.price / price)
                         + max(0.0, entry_slippage)
                         + CONFIG["execution"]["max_slippage_bps"] / 10000)
        qty, _, _ = position_size_from_risk(
            equity, budget / equity, cost_distance, price, cfg["max_implied_leverage"]
        )
        affordable = self.exchange.free / (price * (1 / self.exchange.leverage + self.exchange.fee_rate))
        return min(qty, affordable), plan, liq, price + direction * atr * cfg["tp_atr_mult"]

    async def enter_signal(self, symbol, signal):
        signal_id = uuid4().hex
        if symbol in self.pending_entries or not self.entry_allowed(symbol, time.time()):
            self.record_execution(signal_id, symbol, "entry_capacity_or_cooldown")
            return False
        self.pending_entries.add(symbol)
        try:
            return await self.execute_signal(symbol, signal, signal_id)
        except Exception as exc:
            log.warning("Paper entry unavailable for %s: %s", symbol, exc)
            self.record_execution(signal_id, symbol, "entry_error")
            return False
        finally:
            self.pending_entries.discard(symbol)

    async def execute_signal(self, symbol, signal, signal_id):
        cfg = CONFIG["execution"]
        market_cfg = CONFIG["adaptive"]
        side = "long" if signal["side"] == "buy" else "short"
        rows = list(self.scanner.histories.get(symbol, ()))
        if len(rows) <= market_cfg["atr_period"]:
            rows = await self.exchange.fetch_ohlcv(symbol, "1m", limit=30)
        atr, swing, regime = candle_context(rows, market_cfg["atr_period"], side)
        features = SimpleNamespace(
            side=side,
            cascade_strength=abs(signal["ret"]) * signal["spike"] * abs(signal["imbalance"]),
            volume_ratio=signal["spike"], book_pressure=abs(signal["imbalance"]),
        )
        ctx = SimpleNamespace(regime_fit=regime, cross_venue_agreement=market_cfg["unknown_agreement"])
        weight = quality_gate(features, ctx, CONFIG["quality"])
        if weight == 0:
            self.record_execution(signal_id, symbol, "quality_below_floor")
            return False

        market = MarketView(self.exchange, symbol, side, market_cfg)
        await market.refresh()
        confirm_cfg = {**CONFIG["confirm"], "max_spread": cfg["max_spread"]}

        async def collect_flow():
            while True:
                await market.refresh_flow()
                await asyncio.sleep(market_cfg["book_poll_sec"])

        collector = asyncio.create_task(collect_flow())
        try:
            confirmation = await await_micro_confirmation(features, market, confirm_cfg)
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

        await market.refresh()
        order = plan_entry(side, signal["price"], atr, market.book, cfg)
        multiplier = weight * confirmation["size_mult"]
        qty, _, _, _ = self.risk_plan(side, order["price"], atr, swing, multiplier)
        qty = float(self.exchange.amount_to_precision(symbol, qty))
        if qty <= 0:
            self.record_execution(signal_id, symbol, "quantity_below_precision")
            return False
        order.update(qty=qty, ref_price=signal["price"])
        guard = execution_guard(order, market, cfg)
        if guard:
            self.record_execution(signal_id, symbol, guard)
            return False

        opened_ms = time.time() * 1000
        deadline = market.now() + order["timeout_sec"]
        direction = 1 if side == "long" else -1
        fill_price = None
        fill_type = "GTX"
        entry_slippage = 0.0
        while market.now() < deadline:
            try:
                await asyncio.wait_for(market.refresh_flow(), timeout=deadline - market.now())
            except TimeoutError:
                break
            # A trade through the limit provides more evidence than a touch.
            traded_through = sum(
                float(t["amount"]) for t in market.flow()
                if t["timestamp"] >= opened_ms and t["side"] != signal["side"]
                and direction * (float(t["price"]) - order["price"]) < 0
            )
            if traded_through >= qty:
                fill_price = order["price"]
                break
            await asyncio.sleep(min(market_cfg["book_poll_sec"], max(0, deadline - market.now())))

        if fill_price is None:
            fill_type = "IOC"
            await market.refresh()
            qty = float(self.exchange.amount_to_precision(symbol, qty * order["fallback"]["qty_pct"]))
            if qty <= 0:
                self.record_execution(signal_id, symbol, "quantity_below_precision")
                return False
            guard = execution_guard({**order, "qty": qty}, market, cfg)
            if guard:
                self.record_execution(signal_id, symbol, guard)
                return False
            fill_price = market.sweep(qty, order["fallback"]["price"])
            if fill_price is None:
                self.record_execution(signal_id, symbol, "ioc_unfilled")
                return False
            entry_slippage = market.expected_slippage(qty) / 10000

        async with self.lock:
            now = time.time()
            if not self.entry_allowed(symbol, now):
                self.record_execution(signal_id, symbol, "entry_capacity_or_cooldown")
                return False
            max_qty, stop, liq, target = self.risk_plan(
                side, fill_price, atr, swing, multiplier, entry_slippage
            )
            qty = float(self.exchange.amount_to_precision(symbol, min(qty, max_qty)))
            if qty <= 0:
                self.record_execution(signal_id, symbol, "quantity_below_precision")
                return False
            equity = self.exchange.equity
            order_fill = await self.exchange.create_order(symbol, "market", signal["side"], qty, {
                "reduceOnly": False, "paper_fill_price": fill_price,
            })
            metadata = {
                "signal_id": signal_id, "entry": fill_price, "stop_type": stop.stop_type.value,
                "stop_reason": stop.reason, "qty": qty,
                "implied_leverage": qty * fill_price / equity,
            }
            self.positions[symbol] = {
                **metadata, "side": signal["side"], "original_entry": fill_price,
                "amount": qty, "sl": stop.price, "tp": target, "liq_price": liq,
                "entry_time": now, "entry_fee": order_fill["fee_usdt"], "atr": atr,
                "max_favorable": 0.0, "max_adverse": 0.0, "bars_open": 0,
                "breakeven_armed": False, "stop_kind": "stop_loss",
            }
            self.last_trade[symbol] = now
            self.exchange.trades[-1].update(
                metadata, execution_type=fill_type, confirmed=confirmation["confirmed"],
                quality_multiplier=weight, cross_venue_observed=False,
                initial_risk_usdt=qty * abs(fill_price - stop.price),
            )
            self.record_entry_signal(symbol, signal)
            self.record_execution(signal_id, symbol, "filled")
            return True

    async def adaptive_exits(self):
        symbols = list(self.positions)
        if not symbols:
            return
        quotes = await asyncio.gather(
            *(self.exchange.fetch_ticker(s) for s in symbols), return_exceptions=True
        )
        for symbol, quote in zip(symbols, quotes):
            if isinstance(quote, Exception):
                log.warning("Exit quote unavailable for %s: %s", symbol, quote)
                continue
            await self.evaluate_position(symbol, float(quote["last"]), time.time())

    async def evaluate_position(self, symbol, price, now):
        p = self.positions.get(symbol)
        if p is None:
            return
        side = "long" if p["side"] == "buy" else "short"
        direction = 1 if side == "long" else -1
        pos = Position(side, p["entry"], p["amount"], p["entry_time"], p["sl"], p["tp"],
                       p["signal_id"], p["max_favorable"], p["max_adverse"], p["breakeven_armed"],
                       max(0, int(now // 60 - p["entry_time"] // 60)), p["stop_kind"])
        rows = list(self.scanner.histories.get(symbol, ()))
        if len(rows) > CONFIG["adaptive"]["atr_period"]:
            try:
                p["atr"], _, _ = candle_context(rows, CONFIG["adaptive"]["atr_period"], side)
            except ValueError:
                pass
        alive = self.scanner.position_signal_alive(symbol, p["side"])
        reason = ExitEngine(CONFIG["exit"]).evaluate(
            pos, SimpleNamespace(price=price, atr=p["atr"], now=now), {"signal_alive": alive}
        )
        if direction * (price - p["liq_price"]) <= 0:
            reason = "liquidation"
        p.update(sl=pos.stop, stop_kind=pos.stop_reason, bars_open=pos.bars_open,
                 max_favorable=pos.max_favorable, max_adverse=pos.max_adverse,
                 breakeven_armed=pos.breakeven_armed)
        if reason is None:
            return
        await self.finalize_position(symbol, p, price, now, reason)

    async def finalize_position(self, symbol, p, price, now, reason):
        direction = 1 if p["side"] == "buy" else -1
        fill = await self.exchange.create_order(
            symbol, "market", "sell" if direction == 1 else "buy", p["amount"],
            {"reduceOnly": True, "paper_fill_price": price, "paper_liquidation": reason == "liquidation"},
        )
        self.record_exit_reason(symbol, reason, price)
        self.exchange.trades[-1].update(
            **{k: p[k] for k in ("signal_id", "entry", "stop_type", "qty", "implied_leverage",
                                 "max_favorable", "max_adverse")},
            stop_plan_reason=p["stop_reason"], time_in_trade_sec=now - p["entry_time"],
            pnl_pct=direction * (price - p["entry"]) / p["entry"],
        )
        del self.positions[symbol]
        if fill["pnl_usdt"] - fill["fee_usdt"] - p["entry_fee"] < 0:
            self.last_loss[symbol] = now
