"""Continuous paper runtime for the approved V2 strategy."""

import asyncio
from pathlib import Path
from datetime import datetime
import time

import ccxt.async_support as ccxt

import binance_cascade_hunter_dynamic_v2 as dynamic
from paper_account import PaperState, PaperExchange
from continuous_scanner import ContinuousScanner


class DynamicPaperExchange(PaperExchange):
    async def fetch_tickers(self):
        return await self.market_data.fetch_tickers()


class DynamicPaperHunter(dynamic.CascadeHunter, PaperState):
    def __init__(self, state_path="paper_state_dynamic_v2.json", fee_rate=0.0005,
                 reset=False, market_data=None, strategy_config=dynamic):
        self.strategy = strategy_config
        # Only public market data is used; all fills go through PaperExchange.
        public = market_data if market_data is not None else ccxt.binanceusdm({
            "enableRateLimit": True, "options": {"defaultType": "future"},
        })
        self.exchange = DynamicPaperExchange(public, fee_rate, leverage=self.strategy.LEVERAGE)
        self.positions = {}
        self.last_trade = {}
        self.last_loss = {}
        self.consecutive_losses = 0
        self.equity_peak = self.exchange.equity
        self.lock = asyncio.Lock()
        self.state_path = Path(state_path)
        if self.state_path.exists() and not reset:
            self.restore()
        self.scanner = ContinuousScanner(self, config=self.strategy)

    def settings(self):
        return {"mode": "dynamic_scan", **{name: getattr(self.strategy, name) for name in (
            "LEVERAGE", "FIXED_MARGIN_USDT", "CASCADE_THRESHOLD", "VOLUME_SPIKE",
            "HARD_STOP_PCT", "FAST_ADVERSE_PCT", "PARTIAL_LEVEL", "PARTIAL_FRAC",
            "POST_PARTIAL_STOP_FROM_ENTRY", "POST_PARTIAL_STOP_FROM_PRICE",
            "BREAKEVEN_ARM_PCT", "BREAKEVEN_LOCK_PCT",
            "MIN_SIGNAL_STRENGTH", "SAME_SYMBOL_LOSS_COOLDOWN", "MAX_CONSECUTIVE_LOSSES",
            "SURVIVAL_DRAWDOWN", "SURVIVAL_MARGIN_MULT", "FAST_WINDOW_SEC",
            "COOLDOWN_SEC", "SCAN_INTERVAL",
            "MAX_SYMBOLS_TO_SCAN", "MAX_CONCURRENT_POSITIONS", "MIN_QUOTE_VOLUME",
        )},
                "TRAIL_LEVELS": [list(level) for level in self.strategy.TRAIL_LEVELS]}

    def is_legacy_settings(self, settings):
        current = self.settings()
        keys = ("mode", "position_basis", "stop_basis", "LEVERAGE", "FIXED_MARGIN_USDT",
                "CASCADE_THRESHOLD", "VOLUME_SPIKE", "COOLDOWN_SEC", "SCAN_INTERVAL",
                "MAX_SYMBOLS_TO_SCAN", "MAX_CONCURRENT_POSITIONS", "MIN_QUOTE_VOLUME")
        legacy = {key: current[key] for key in keys if key in current}
        legacy.update(STOP_LOSS_PCT=0.03, PARTIAL_LOSS_PCT=0.015,
                      TRAIL_START_PCT=0.01, TRAIL_STEP_PCT=0.005)
        return settings == legacy

    def migrate_legacy_positions(self):
        for symbol, p in self.positions.items():
            fills = []
            for fill in reversed(self.exchange.trades):
                if fill["symbol"] == symbol:
                    fills.append(fill)
                    if not fill["reduceOnly"]:
                        break
            entry = p["entry"]
            direction = 1 if p["side"] == "buy" else -1
            partial = p.pop("partial_closed")
            lock = p.pop("trail_level") * 0.005
            p.pop("tp", None)
            opened = datetime.fromisoformat(fills[-1]["timestamp"]).timestamp() if fills else time.time()
            p.update(original_entry=entry, entry_time=opened, partial_done=partial,
                     trail_locked=lock, max_adverse=0.0, max_favorable=0.0,
                     fast_checked=time.time() - opened > self.strategy.FAST_WINDOW_SEC,
                     stop_kind="TRAILING_SL" if lock else "POST_PARTIAL" if partial else "SL",
                     net_pnl=sum(f["pnl_usdt"] - f["fee_usdt"] for f in fills))
            stop = self.strategy.HARD_STOP_PCT
            if partial:
                stop = self.strategy.POST_PARTIAL_STOP_FROM_ENTRY
            self.tighten_stop(p, entry * (1 - direction * stop), p["stop_kind"])

    async def enter(self, symbol, side, price):
        # ContinuousScanner owns the entry lock; do not acquire it again here.
        result = await self.strategy.CascadeHunter.enter(self, symbol, side, float(price))
        self.save()
        return result

    def record_entry_signal(self, symbol, signal):
        for trade in reversed(self.exchange.trades):
            if trade["symbol"] == symbol and not trade["reduceOnly"]:
                trade["entry_signal"] = dict(signal)
                trade["entry_reason"] = "CASCADE_MOVE_VOLUME_ORDERBOOK"
                position = self.positions[symbol]
                trade["stop_loss_price"] = position["sl"]
                self.save()
                break

    def record_exit_reason(self, symbol, reason, trigger_price):
        for trade in reversed(self.exchange.trades):
            if trade["symbol"] == symbol and trade["reduceOnly"]:
                trade["exit_reason"] = reason
                trade["trigger_price"] = float(trigger_price)
                break

    async def manage_exits(self):
        await self.strategy.CascadeHunter.manage_exits(self)
        self.save()

    async def scan_loop(self):
        await self.scanner.run()

    async def start(self):
        tasks = []
        try:
            await self.exchange.load_markets()
            self.save()
            dynamic.log.info("CONTINUOUS PAPER ONLY | initial=100 USDT | wallet=%.4f | state=%s",
                             self.exchange.balance, self.state_path)
            tasks = [asyncio.create_task(self.scan_loop()),
                     asyncio.create_task(self.exit_watcher()),
                     asyncio.create_task(self.report())]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                self.save()
            finally:
                await self.exchange.close()


