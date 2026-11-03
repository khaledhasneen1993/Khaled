import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import binance_cascade_hunter_dynamic_v2 as v2
from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
from continuous_scanner import ContinuousScanner
from test_continuous import NOW, StreamData, event

BTC = 'BTC/USDT:USDT'

class V2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.data = StreamData()
        self.bot = V2PaperHunter(self.path, fee_rate=0, market_data=self.data)

    async def tick(self, price):
        self.data.price = price
        await self.bot.manage_exits()

    async def test_four_positions_then_reject_fifth_and_duplicate(self):
        for i in range(4):
            symbol = f"COIN{i}/USDT:USDT"
            self.assertTrue(await self.bot.enter(symbol, "buy", 100))
            self.assertAlmostEqual(self.bot.positions[symbol]["amount"], 5)
            self.assertAlmostEqual(self.bot.exchange.positions[symbol]["margin"], 10)
        self.assertFalse(await self.bot.enter(BTC, "buy", 100))
        self.assertFalse(await self.bot.enter("COIN0/USDT:USDT", "sell", 100))
        self.assertEqual(len(self.bot.positions), 4)

    async def test_trail_arms_at_threshold_and_tracks_best_price(self):
        for side, direction in [("buy", 1), ("sell", -1)]:
            with self.subTest(side=side):
                self.bot = V2PaperHunter(self.path, fee_rate=0, reset=True, market_data=self.data)
                self.data.price = 100
                await self.bot.enter(BTC, side, 100)
                p = self.bot.positions[BTC]
                await self.tick(100 * (1 + direction * .0149))
                self.assertFalse(p["trailing"])
                self.assertAlmostEqual(p["sl"], 100 * (1 - direction * .08))
                await self.tick(100 * (1 + direction * .015))
                self.assertTrue(p["trailing"])
                self.assertAlmostEqual(p["sl"], p["best_price"] * (1 - direction * .006))
                best = 100 * (1 + direction * .03)
                await self.tick(best)
                stop = best * (1 - direction * .006)
                await self.tick(100 * (1 + direction * .027))
                self.bot = V2PaperHunter(self.path, fee_rate=0, market_data=self.data)
                self.assertAlmostEqual(self.bot.positions[BTC]["best_price"], best)
                self.assertAlmostEqual(self.bot.positions[BTC]["sl"], stop)
                await self.tick(stop)
                self.assertFalse(self.bot.positions)
                self.assertEqual(len(self.bot.exchange.trades), 2)
                self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "TRAILING_SL")

    async def test_adverse_path_closes_only_at_hard_stop(self):
        for side, direction in [("buy", 1), ("sell", -1)]:
            with self.subTest(side=side):
                self.bot = V2PaperHunter(self.path, fee_rate=0, reset=True, market_data=self.data)
                self.data.price = 100
                await self.bot.enter(BTC, side, 100)
                for loss in (.01, .015, .028, .03, .079):
                    await self.tick(100 * (1 - direction * loss))
                    self.assertEqual(len(self.bot.exchange.trades), 1)
                    self.assertEqual(self.bot.positions[BTC]["amount"], 5)
                await self.tick(100 * (1 - direction * .08))
                self.assertFalse(self.bot.positions)
                self.assertAlmostEqual(self.bot.exchange.balance, 60)
                self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "SL")

    async def test_account_rejects_reducing_less_than_full_position(self):
        await self.bot.enter(BTC, "buy", 100)
        with self.assertRaises(ValueError):
            await self.bot.exchange.create_order(BTC, "market", "sell", 2.5,
                                                 params={"reduceOnly": True})
        self.assertEqual(self.bot.exchange.positions[BTC]["amount"], 5)

    async def test_size_compounds_after_realized_profit(self):
        await self.bot.enter(BTC, "buy", 100)
        await self.tick(103)
        await self.tick(102)
        self.assertAlmostEqual(self.bot.exchange.balance, 110)
        self.data.price = 100
        self.assertTrue(await self.bot.enter("ETH", "buy", 100))
        self.assertAlmostEqual(self.bot.exchange.positions["ETH"]["margin"], 11)

    async def test_free_margin_rejects_entry(self):
        await self.bot.enter(BTC, "buy", 100)
        await self.tick(93)
        self.assertEqual(self.bot.exchange.balance, 100)
        # Simulate other reserved funds without changing the wallet basis.
        self.bot.exchange.positions["reserved"] = dict(amount=0, side="buy", entry=100, margin=85)
        self.assertFalse(await self.bot.enter("ETH", "buy", 93))

    async def test_loss_cooldown_is_symbol_local_and_persisted(self):
        with patch.object(v2.time, "time", return_value=1000):
            await self.bot.enter(BTC, "buy", 100)
            await self.tick(92)
        self.bot = V2PaperHunter(self.path, fee_rate=0, market_data=self.data)
        self.assertEqual(self.bot.last_loss[BTC], 1000)
        self.data.price = 100
        with patch.object(v2.time, "time", return_value=1599):
            self.assertFalse(await self.bot.enter(BTC, "buy", 100))
            self.assertTrue(await self.bot.enter("ETH", "buy", 100))
        with patch.object(v2.time, "time", return_value=1600):
            self.assertTrue(await self.bot.enter(BTC, "buy", 100))

    async def test_scanner_rejects_weak_otherwise_valid_signal(self):
        scanner = ContinuousScanner(self.bot, clock=lambda:100, wall_clock=lambda:NOW/1000)
        await scanner.refresh_universe()
        await scanner.seed(scanner.symbols)
        scanner.connected = True
        async def weak_book(*args, **kwargs):
            return {"bids": [[100, 68]], "asks": [[100, 32]]}
        self.data.fetch_order_book = weak_book
        scanner.ingest(event("BTCUSDT", close=101.3, volume=4))
        await scanner.evaluate(BTC)
        self.assertEqual(scanner.book_checks, 1)
        self.assertFalse(self.bot.positions)

    async def test_incompatible_state_rejected_without_modifying_file(self):
        self.bot.save()
        state=json.loads(self.path.read_text())
        state['settings']['MAX_POSITION_PCT']=.99
        self.path.write_text(json.dumps(state))
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):V2PaperHunter(self.path,fee_rate=0,market_data=self.data)
        self.assertEqual(self.path.read_bytes(),before)

    async def test_scanner_enforces_four_positions_concurrently(self):
        self.data.tickers={f'COIN{i}/USDT:USDT':{'quoteVolume':25_000_000} for i in range(7)}
        scanner=ContinuousScanner(self.bot,config=v2,clock=lambda:100,wall_clock=lambda:NOW/1000)
        await scanner.refresh_universe()
        await scanner.seed(scanner.symbols)
        scanner.connected=True
        self.data.price=102
        for i in range(7):scanner.ingest(event(f'COIN{i}USDT'))
        with patch('continuous_scanner.BOOK_REQUEST_SPACING',0):
            await asyncio.gather(*(scanner.evaluate(s) for s in scanner.symbols))
        self.assertEqual(len(self.bot.positions),4)
