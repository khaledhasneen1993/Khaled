import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from execution.adaptive_runtime import CONFIG
from test_continuous import BTC, StreamData, event


class AdaptiveAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(CONFIG["confirm"], confirm_timeout_sec=0).start()
        patch.dict(CONFIG["execution"], entry_timeout_sec=0).start()
        self.path = Path(temp.name) / "state.json"
        self.data = StreamData()
        self.bot = V2PaperHunter(self.path, market_data=self.data)
        sc = self.bot.scanner
        await sc.refresh_universe()
        await sc.seed(sc.symbols)
        sc.connected = True
        sc.wall_clock = lambda: 1800000000

    async def test_gap_liquidation_records_margin_loss_and_symbol_cooldown(self):
        self.data.price = 102
        self.bot.scanner.ingest(event())
        await self.bot.scanner.evaluate(BTC)
        pos = self.bot.positions[BTC]
        margin = self.bot.exchange.positions[BTC]["margin"]
        self.data.price = pos["liq_price"] - .1
        await self.bot.manage_exits()
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "liquidation")
        self.assertAlmostEqual(self.bot.exchange.trades[-1]["pnl_usdt"], -margin)
        self.assertFalse(self.bot.positions)
        restored = V2PaperHunter(self.path, market_data=self.data)
        self.assertIn(BTC, restored.last_loss)
        self.assertFalse(restored.entry_allowed(BTC, restored.last_loss[BTC] + 599))

    async def test_incompatible_state_refused_without_overwrite(self):
        self.bot.save()
        source = self.path.read_text()
        self.path.write_text(source.replace('"adaptive_v1"', '"previous_model"'))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            V2PaperHunter(self.path, market_data=self.data)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_sizing_reserves_both_fees_and_slippage(self):
        equity = self.bot.exchange.equity
        qty, stop, _, _ = self.bot.risk_plan("short", 100, 1, None, 1.0)
        cost = (abs(100 - stop.price) + (100 + stop.price) * self.bot.exchange.fee_rate
                + 100 * CONFIG["execution"]["max_slippage_bps"] / 10000)
        self.assertAlmostEqual(qty * cost, equity * CONFIG["risk"]["risk_pct_per_trade"])

    async def test_passive_trade_through_fills_at_limit(self):
        import time

        async def prints(symbol, since, limit):
            return [{"timestamp": time.time() * 1000, "side": "sell",
                     "price": 101.98, "amount": 1000}]

        self.data.fetch_trades = prints
        self.data.price = 102
        self.bot.scanner.ingest(event())
        with patch.dict(CONFIG["execution"], entry_timeout_sec=5):
            await self.bot.scanner.evaluate(BTC)
        fill = self.bot.exchange.trades[-1]
        self.assertEqual(fill["execution_type"], "GTX")
        self.assertAlmostEqual(fill["price"], 101.99)
        self.assertEqual(self.bot.positions[BTC]["entry"], fill["price"])

    async def test_realistic_rest_delay_still_reaches_ioc_fill(self):
        import asyncio

        original = self.data.fetch_order_book

        async def delayed_book(*args, **kwargs):
            await asyncio.sleep(.25)
            return await original(*args, **kwargs)

        self.data.fetch_order_book = delayed_book
        self.data.price = 102
        self.bot.scanner.ingest(event())
        await self.bot.scanner.evaluate(BTC)
        self.assertEqual(self.bot.exchange.trades[-1]["execution_type"], "IOC")
        self.assertIn(BTC, self.bot.positions)

    async def test_prior_latency_settings_resume_but_risk_changes_do_not(self):
        import json

        self.bot.save()
        state = json.loads(self.path.read_text())
        state["settings"]["adaptive_config"]["execution"]["max_latency_ms"] = 200
        self.path.write_text(json.dumps(state))
        old_bytes = self.path.read_bytes()
        restored = V2PaperHunter(self.path, market_data=self.data)
        self.assertEqual(restored.exchange.balance, self.bot.exchange.balance)
        self.assertEqual(restored.exchange.trades, self.bot.exchange.trades)
        self.assertEqual(self.path.read_bytes(), old_bytes)
        state["settings"]["adaptive_config"]["risk"]["risk_pct_per_trade"] = .1
        self.path.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            V2PaperHunter(self.path, market_data=self.data)
