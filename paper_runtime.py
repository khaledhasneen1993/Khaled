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
        result = {"mode": "dynamic_scan", "execution_mode": self.execution_mode, "active_exit": {key: self.cfg[key] for key in ("tp_sl", "atr", "exit")},
                **{name: getattr(self.strategy, name) for name in (
            "LEVERAGE", "MAX_POSITION_PCT", "CASCADE_THRESHOLD", "VOLUME_SPIKE",
            "COOLDOWN_SEC", "SCAN_INTERVAL",
            "MAX_SYMBOLS_TO_SCAN", "MAX_CONCURRENT_POSITIONS", "MIN_QUOTE_VOLUME",
        )}}
        if self.execution_mode != 'paper': result['LEVERAGE'] = self.cfg['exchange']['leverage_policy']
        return result

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

    def merge_execution_state(self):
        """Merge exchange facts while retaining local strategy/dynamic stop metadata."""
        from paper_account import Position
        live = self.exchange
        seen = set()
        for record in reversed(list(live.executions.values())):
            pos = record.get('position')
            symbol = record['symbol']
            if pos and symbol in live.positions and symbol not in seen:
                seen.add(symbol)
                if symbol not in self.positions:
                    self.positions[symbol] = Position(pos)
                else:
                    sm = 1 if pos['side'] == 'buy' else -1
                    if sm * (pos['sl'] - self.positions[symbol]['sl']) > 0:
                        self.positions[symbol]['sl'] = pos['sl']
                    self.positions[symbol]['entry_fee'] = pos['entry_fee']
                    self.positions[symbol]['accounting_status'] = pos.get('accounting_status', 'pending')
        for trade in live.trades:
            if trade.get('exit_position') and trade.get('accounting_status') == 'complete' and not trade.get('exit_detail'):
                stored = trade['exit_position']
                entry = next((r.get('position') for r in live.executions.values()
                              if r.get('position', {}).get('signal_id') == stored['signal_id']), None)
                closes = [t for t in live.trades if t.get('reduceOnly') and t.get('signal_id') == stored['signal_id']]
                if entry and entry.get('accounting_status') == 'complete' and all(t.get('accounting_status') == 'complete' for t in closes):
                    stored['entry_fee'] = entry['entry_fee']
                    fee = sum(t['fee_usdt'] for t in closes)
                    amount = sum(t['amount'] for t in closes)
                    price = sum(t['amount'] * t['price'] for t in closes) / amount
                    trade['exit_detail'] = exit_record(Position(stored), trade['exit_reason'], price,
                        datetime.fromisoformat(trade['timestamp']).timestamp(), fee)
                    trade['exit_detail']['net_pnl_usdt'] = sum(t['pnl_usdt'] for t in closes) - fee - entry['entry_fee']
                    self._notifications.append(trade)
            if trade.get('reduceOnly') and trade.get('exit_reason'):
                symbol = trade['symbol']
                if symbol in self.positions and symbol not in live.positions:
                    self.record_exit_reason(symbol, trade['exit_reason'], trade['price'])

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
                if (position.get('accounting_status') == 'pending'
                        or any(t.get('accounting_status') == 'pending' for t in closes)):
                    trade['accounting_status'] = 'pending'
                    trade['exit_position'] = dict(position)
                    return
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
            await self.exchange.settle_before_close(symbol)
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
                                          "signal_id": self.positions.get(symbol, {}).get("signal_id"),
                                          "exit_reason": reason, "exit_position": self.positions.get(symbol)})
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
        self.persist_execution_metadata()
        await self._drain_notifications()
        self.save()

    def persist_execution_metadata(self):
        if self.execution_mode == 'paper': return
        for trade in self.exchange.trades:
            record = self.exchange.executions.get(self.exchange.execution_key(trade['symbol'], trade['id']))
            if record:
                record['trade'] = dict(trade)
                pos = self.positions.get(trade['symbol'])
                if record.get('position') and pos and record['position']['signal_id'] == pos['signal_id']:
                    record['position'] = dict(pos)
        self.exchange._flush_executions()

    async def _drain_notifications(self):
        while self._notifications:
            await self.safety.closed(self._notifications.pop(0))

    async def adopt_native_close(self, symbol):
        if self.execution_mode == 'paper': return False
        pos = self.positions[symbol]
        # An explicit close can have succeeded before its local state write.
        await self.exchange.recover_execution()
        closes = [t for t in self.exchange.trades if t.get('reduceOnly') and t['symbol'] == symbol
                  and t.get('signal_id') == pos['signal_id']]
        reason = 'recovered_close'
        if not closes:
            for name, protection in pos.get('protection', {}).items():
                data = await self.exchange.query_order_by_client_id(symbol, protection['clientOrderId'])
                if not data or not data.get('actualOrderId'): continue
                order = await self.exchange._request('GET', '/fapi/v1/order', {
                    'symbol': self.exchange.raw(symbol), 'orderId': data['actualOrderId']}, retry=True)
                order = await self.exchange.settle_fill(symbol, order)
                if float(order.get('executedQty', 0)) <= 0: continue
                await self.exchange._accept_fill(symbol, order, {'reduceOnly': True,
                    'signal_id': pos['signal_id'], 'expected_price': float(protection['trigger'])},
                    order['side'].lower(), recovered=True)
                reason = 'SL' if name == 'sl' else 'TP'
                closes = [self.exchange.trades[-1]]
                break
        if not closes:
            # A remote reducing execution may be absent from both local intents
            # and native protection metadata. Discover it without sending orders.
            fills = await self.exchange._request('GET', '/fapi/v1/userTrades', {
                'symbol': self.exchange.raw(symbol), 'startTime': int(pos['opened_at'] * 1000),
                'limit': self.cfg['exchange']['trade_page_limit']}, retry=True)
            for order_id in dict.fromkeys(str(f['orderId']) for f in fills if f.get('orderId')):
                order = await self.exchange._request('GET', '/fapi/v1/order', {
                    'symbol': self.exchange.raw(symbol), 'orderId': order_id}, retry=True)
                if order.get('reduceOnly') not in (True, 'true') or order.get('status') not in ('FILLED', 'EXPIRED', 'CANCELED'): continue
                if float(order.get('executedQty', 0)) <= 0: continue
                await self.exchange._accept_fill(symbol, order, {'reduceOnly': True,
                    'signal_id': pos['signal_id'], 'exit_position': dict(pos)}, order['side'].lower(), recovered=True)
            closes = [t for t in self.exchange.trades if t.get('reduceOnly') and t.get('signal_id') == pos['signal_id']]
        if not closes: return False
        quantity = sum(t['amount'] for t in self.exchange.trades if t.get('reduceOnly')
                       and t.get('signal_id') == pos['signal_id'])
        if not math.isclose(quantity, pos['amount'], rel_tol=self.cfg['exchange']['fill_qty_tolerance_pct']):
            return False
        self.record_exit_reason(symbol, reason, closes[-1]['price'])
        self.persist_execution_metadata()
        self.positions.pop(symbol)
        self.exchange.pending_protection.pop(symbol, None)
        for remaining in await self.exchange.get_open_orders(symbol):
            if remaining.get('is_algo'): await self.exchange.cancel_algo(remaining)
        await self._drain_notifications()
        self.save()
        return True

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

