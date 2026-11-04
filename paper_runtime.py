"""Continuous paper runtime for Khaled with adaptive exits."""

import asyncio
from pathlib import Path

import ccxt.async_support as ccxt

import binance_cascade_hunter_dynamic_v2 as dynamic
from paper_account import PaperState, PaperExchange
from continuous_scanner import ContinuousScanner
from risk.config import load_config
from journal.trade_journal import exit_record
from datetime import datetime, timezone
import fcntl
import time
import math
import sys
from infra.supervisor import SafetySupervisor
from risk.liq_math import direction
from risk.slippage import exit_slippage_bps


class DynamicPaperExchange(PaperExchange):
    async def fetch_tickers(self):
        return await self.market_data.fetch_tickers()


class DynamicPaperHunter(dynamic.CascadeHunter, PaperState):
    def __init__(self, state_path="paper_state_dynamic_v2.json", fee_rate=0.0005,
                 reset=False, market_data=None, strategy_config=dynamic):
        self.strategy = strategy_config
        self.cfg = load_config()
        self.execution_mode = self.cfg["exchange"]["mode"]
        if self.execution_mode not in ("paper", "testnet", "mainnet"):
            raise ValueError("Invalid exchange mode")
        if self.execution_mode != "paper" and reset:
            raise ValueError("Live state cannot be reset; reconcile and review it")
        # A single config mode selects both data and execution environments.
        if self.execution_mode == "paper":
            public = market_data if market_data is not None else ccxt.binanceusdm({
                "enableRateLimit": True, "options": {"defaultType": "future"},
            })
            self.exchange = DynamicPaperExchange(public, fee_rate, leverage=self.strategy.LEVERAGE)
        else:
            if market_data is not None:
                raise ValueError("Injected public paper clients cannot be used in live modes")
            from exchange.live_exchange import LiveExchange
            from infra.rate_limit import TokenBucket
            ec = self.cfg["exchange"]
            self.exchange = LiveExchange(self.cfg, TokenBucket(ec["rate_per_sec"], ec["rate_burst"]))
        self.exchange.cfg = self.cfg
        self.initial_balance = self.cfg["initial_balance_usdt"] if self.execution_mode == "paper" else None
        self.positions = {}
        self.last_trade = {}
        self.lock = asyncio.Lock()
        self.state_path = Path(state_path)
        if self.execution_mode != "paper":
            self.state_path = Path(self.cfg["exchange"]["state_dir"]) / self.execution_mode / self.state_path.name
        self._notifications = []
        self._state_lock = None
        self._owns_state = self.execution_mode == "paper"
        self.safety = SafetySupervisor(self)
        if self.execution_mode != "paper":
            self.exchange.on_api_error = self.safety.kill.on_api_error
        if self.state_path.exists() and not reset:
            self.restore()
        self.scanner = ContinuousScanner(self, config=self.strategy)

    def settings(self):
        return {"mode": "dynamic_scan", "execution_mode": self.execution_mode, "active_exit": {key: self.cfg[key] for key in ("tp_sl", "atr", "exit")},
                **{name: getattr(self.strategy, name) for name in (
            "LEVERAGE", "MAX_POSITION_PCT", "CASCADE_THRESHOLD", "VOLUME_SPIKE",
            "COOLDOWN_SEC", "SCAN_INTERVAL",
            "MAX_SYMBOLS_TO_SCAN", "MAX_CONCURRENT_POSITIONS", "MIN_QUOTE_VOLUME",
        )}}

    async def enter(self, symbol, side, price):
        # ContinuousScanner owns the entry lock; do not acquire it again here.
        if self.execution_mode != "paper":
            if not self.safety.ready:
                return False
            await self.exchange.refresh_account()
            remote = self.exchange.positions
            mismatch = set(remote) != set(self.positions)
            if not mismatch:
                for current_symbol, actual in remote.items():
                    local = self.positions[current_symbol]
                    if (actual["side"] != local["side"]
                            or not math.isclose(actual["amount"], local["amount"], rel_tol=self.cfg["recon"]["size_tolerance_pct"])
                            or not math.isclose(actual["entry"], local["entry"], rel_tol=self.cfg["recon"]["entry_tolerance_pct"])):
                        mismatch = True
                        break
            if mismatch and not await self.safety.reconcile_once():
                return False
        if (self.safety.active or self.execution_mode != "paper") and not await self.safety.allow_entry(symbol, side, price):
            return False
        result = await self.strategy.CascadeHunter.enter(self, symbol, side, float(price))
        if getattr(self.exchange, "execution_fault", None):
            # CascadeHunter preserves its public False-on-failure contract. The
            # live runtime still latches and handles actual possible fills.
            for pending_symbol, pending in self.exchange.pending_protection.items():
                if pending_symbol not in self.positions:
                    self.positions[pending_symbol] = pending
            await self.safety.trip(self.exchange.execution_fault)
        elif self.safety.kill.triggered and self.safety.active:
            await self.safety.trip(self.safety.kill.triggered)
            result = False
        elif result and self.safety.active:
            await self.safety.event("trade_opened", symbol=symbol, reason=side)
        self.save()
        return result

    def record_entry_signal(self, symbol, signal):
        for trade in reversed(self.exchange.trades):
            if trade["symbol"] == symbol and not trade["reduceOnly"]:
                trade["entry_signal"] = dict(signal)
                trade["entry_reason"] = "CASCADE_MOVE_VOLUME_ORDERBOOK"
                position = self.positions[symbol]
                trade["take_profit_price"] = position["tp"]
                trade["stop_loss_price"] = position["sl"]
                self.save()
                break

    def record_exit_reason(self, symbol, reason, trigger_price):
        for trade in reversed(self.exchange.trades):
            if trade["symbol"] == symbol and trade["reduceOnly"]:
                trade["exit_reason"] = reason
                trade["trigger_price"] = float(trigger_price)
                position = self.positions[symbol]
                closes = [t for t in self.exchange.trades if t.get("reduceOnly")
                          and t.get("signal_id") == position["signal_id"]]
                if not closes: closes = [trade]
                quantity = sum(t["amount"] for t in closes)
                weighted_exit = sum(t["price"] * t["amount"] for t in closes) / quantity
                total_fees = sum(t["fee_usdt"] for t in closes)
                trade["exit_detail"] = exit_record(
                    position, {"SL": "stop_loss", "TP": "take_profit"}.get(reason, reason),
                    weighted_exit, datetime.fromisoformat(trade["timestamp"]).timestamp(), total_fees)
                trade["exit_detail"]["net_pnl_usdt"] = sum(t["pnl_usdt"] for t in closes) - position["entry_fee"] - total_fees
                for key in ("expected_entry", "actual_entry", "entry_slippage_bps"):
                    trade[key] = position.get(key, position["entry"] if key != "entry_slippage_bps" else 0.)
                    trade["exit_detail"][key] = trade[key]
                for key in ("expected_exit", "actual_exit", "exit_slippage_bps"):
                    if key in trade: trade["exit_detail"][key] = trade[key]
                trade["exit_detail"]["actual_exit"] = weighted_exit
                self.safety.kill.on_trade_closed(trade["exit_detail"]["net_pnl_usdt"]) if not self.safety.active else None
                if self.safety.active: self._notifications.append(trade)
                break

    @property
    def open_positions(self): return self.positions

    @property
    def equity(self): return self.exchange.equity

    @property
    def open_orders(self): return self.safety.open_orders

    def symbols(self): return tuple(self.positions)

    def remove_position(self, symbol):
        self.positions.pop(symbol, None)

    def on_market_heartbeat(self): self.safety.heartbeat()

    def on_order_book(self, symbol, book): self.safety.observe_book(symbol, book)

    async def close_position(self, symbol, reason, trigger_price=None):
        if self.execution_mode != "paper":
            await self.exchange.refresh_account()
        account = self.exchange.positions.get(symbol)
        if account is None:
            if symbol in self.positions and await self.adopt_native_close(symbol): return
            if symbol in self.positions: raise RuntimeError("Remote close requires reconciliation")
            return
        price = trigger_price if trigger_price is not None else (await self.exchange.fetch_ticker(symbol))["last"]
        previous_fault = getattr(self.exchange, "execution_fault", None)
        try:
            await self.exchange.create_order(symbol, "market", "sell" if account["side"] == "buy" else "buy",
                account["amount"], params={"reduceOnly": True, "expected_price": price,
                                          "signal_id": self.positions.get(symbol, {}).get("signal_id")})
        except Exception:
            if self.execution_mode != "paper":
                await self.exchange.refresh_account()
                if symbol not in self.exchange.positions and symbol in self.positions and await self.adopt_native_close(symbol):
                    self.exchange.execution_fault = previous_fault
                    return
            raise
        if symbol in self.positions:
            self.record_exit_reason(symbol, reason, price)
            self.positions.pop(symbol, None)
        if self.execution_mode != "paper":
            for order in await self.exchange.get_open_orders(symbol):
                if order.get("is_algo"): await self.exchange.cancel_algo(order)
                else: await self.exchange.cancel_order(symbol, order_id=order["orderId"])
        await self._drain_notifications()
        self.save()

    async def _drain_notifications(self):
        while self._notifications:
            await self.safety.closed(self._notifications.pop(0))

    async def adopt_native_close(self, symbol):
        if self.execution_mode == "paper": return False
        pos = self.positions[symbol]
        for name, protection in pos.get("protection", {}).items():
            data = await self.exchange.query_order_by_client_id(symbol, protection["clientOrderId"])
            if not data or not data.get("actualOrderId"): continue
            order = await self.exchange._request("GET", "/fapi/v1/order", {"symbol": self.exchange.raw(symbol), "orderId": data["actualOrderId"]}, retry=True)
            order = await self.exchange.settle_fill(symbol, order)
            if abs(float(order["executedQty"]) - pos["amount"]) > self.cfg["recon"]["size_tolerance_pct"] * pos["amount"]:
                continue
            fills = await self.exchange._request("GET", "/fapi/v1/userTrades", {"symbol": self.exchange.raw(symbol), "orderId": order["orderId"], "limit": self.cfg["exchange"]["trade_page_limit"]}, retry=True)
            if not fills or any(f["commissionAsset"] != "USDT" for f in fills): return False
            if not math.isclose(sum(float(f["qty"]) for f in fills), float(order["executedQty"]),
                                rel_tol=self.cfg["exchange"]["fill_qty_tolerance_pct"], abs_tol=0): return False
            actual = float(order["avgPrice"])
            expected = float(protection["trigger"])
            trade = {"id": str(order["orderId"]), "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "mode": self.execution_mode, "signal_id": pos["signal_id"], "side": order["side"].lower(), "amount": float(order["executedQty"]),
                "price": actual, "reduceOnly": True, "fee_usdt": sum(float(f["commission"]) for f in fills),
                "pnl_usdt": sum(float(f["realizedPnl"]) for f in fills), "balance_usdt": self.exchange.balance,
                "expected_exit": expected, "actual_exit": actual,
                "exit_slippage_bps": exit_slippage_bps(expected, actual, pos["side"])}
            if any(t["id"] == trade["id"] and t.get("reduceOnly") for t in self.exchange.trades): return False
            self.exchange.trades.append(trade)
            self.record_exit_reason(symbol, "SL" if name == "sl" else "TP", actual)
            self.positions.pop(symbol)
            for remaining in await self.exchange.get_open_orders(symbol):
                if remaining.get("is_algo"): await self.exchange.cancel_algo(remaining)
            await self._drain_notifications()
            self.save()
            return True
        return False

    async def manage_exits(self):
        if self.execution_mode != "paper":
            await self.exchange.refresh_account()
            if not await self.safety.reconcile_once(): return
            for symbol, pos in self.positions.items():
                remote = self.exchange.positions.get(symbol, {}).get("remote", {})
                liquidation = float(remote.get("liquidationPrice", 0))
                buffer = self.cfg["tp_sl"]["liq_safety_margin"]
                safe = (pos["sl"] > liquidation * (1 + buffer) if pos["side"] == "buy"
                        else pos["sl"] < liquidation * (1 - buffer))
                if liquidation <= 0 or not safe:
                    await self.safety.trip("liquidation_guard_breach")
                    return
        await self.strategy.CascadeHunter.manage_exits(self)
        # P2 follows existing TP/SL checks without altering their calculations.
        for symbol, pos in list(self.positions.items()):
            price = self.exchange.prices.get(symbol, pos["entry"])
            pnl_pct = direction(pos["side"]) * (price / pos["entry"] - 1)
            age = time.time() - pos["opened_at"]
            if self.cfg["time_stop"]["enabled"] and age >= self.cfg["time_stop"]["minutes"] * 60 and pnl_pct < self.cfg["time_stop"]["min_profit_pct"]:
                await self.close_position(symbol, "time_stop", price)
            elif self.execution_mode != "paper":
                try: await self.exchange.protect(symbol, pos)
                except Exception: await self.safety.trip("protection_update_failed")
        await self._drain_notifications()
        self.save()

    async def scan_loop(self):
        await self.scanner.run()

    async def start(self):
        tasks = []
        try:
            if self.execution_mode != "paper":
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_lock = (self.state_path.parent / "account.lock").open("a")
                try: fcntl.flock(self._state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError: raise ValueError("Another process owns this live account") from None
                self._owns_state = True
            await self.exchange.load_markets()
            if self.initial_balance is None: self.initial_balance = self.exchange.equity
            await self.safety.startup()
            self.save()
            dynamic.log.info("CONTINUOUS %s | equity=%.4f | state=%s", self.execution_mode,
                             self.exchange.equity, self.state_path)
            tasks = [asyncio.create_task(self.scan_loop()),
                     asyncio.create_task(self.exit_watcher()),
                     asyncio.create_task(self.report()), asyncio.create_task(self.safety.monitor()),
                     asyncio.create_task(self.safety.watchdog()),
                     asyncio.create_task(self.safety.alerter._worker())]
            await asyncio.gather(*tasks)
        finally:
            cause = sys.exc_info()[0]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if self._owns_state and self.execution_mode != "paper" and getattr(self.exchange, "markets", None):
                    shutdown_worker = asyncio.create_task(self.safety.alerter._worker())
                    try:
                        reason = "process_shutdown" if cause in (None, asyncio.CancelledError, KeyboardInterrupt) else "runtime_failure"
                        await self.safety.trip(reason)
                        if self.safety.alerter.enabled:
                            await asyncio.wait_for(self.safety.alerter.queue.join(), self.cfg["alerts"]["shutdown_timeout_sec"])
                    except Exception:
                        if self.safety.journal: self.safety.journal.log("shutdown_incomplete")
                    finally:
                        shutdown_worker.cancel()
                        await asyncio.gather(shutdown_worker, return_exceptions=True)
                if self._owns_state: self.save()
            finally:
                await self.exchange.close()
                if self._state_lock:
                    self._state_lock.close()

