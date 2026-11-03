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

    async def test_six_fixed_margin_positions_then_reject_seventh(self):
        for i in range(6):
            symbol = f'COIN{i}/USDT:USDT'
            self.assertTrue(await self.bot.enter(symbol, 'buy', 100))
            self.assertAlmostEqual(self.bot.positions[symbol]['amount'], 1)
            self.assertAlmostEqual(self.bot.exchange.positions[symbol]['margin'], 10)
        self.assertFalse(await self.bot.enter(BTC, 'buy', 100))
        self.assertEqual(len(self.bot.positions), 6)
        self.assertAlmostEqual(self.bot.exchange.used, 60)

    async def test_trailing_staircase_long_and_short(self):
        for side, direction in [('buy', 1), ('sell', -1)]:
            with self.subTest(side=side):
                self.bot = V2PaperHunter(self.path, fee_rate=0, reset=True, market_data=self.data)
                self.data.price = 100
                await self.bot.enter(BTC, side, 100)
                for gain, stop in [(.0079,-.028),(.008,0),(.012,.005),(.018,.01),(.025,.015),(.035,.022),(.05,.03),(.031,.03)]:
                    await self.tick(100 * (1 + direction * gain))
                    self.assertAlmostEqual(self.bot.positions[BTC]['sl'],100*(1+direction*stop))
                await self.tick(100 * (1 + direction * .03))
                self.assertFalse(self.bot.positions)
                self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TRAILING_SL')
                self.assertAlmostEqual(self.bot.exchange.balance,103)

    async def test_partial_once_then_recovery_trailing_with_restore(self):
        for side,direction in [('buy',1),('sell',-1)]:
            with self.subTest(side=side):
                self.data.price=100
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,reset=True,market_data=self.data)
                await self.bot.enter(BTC,side,100)
                self.bot.positions[BTC]["entry_time"] -= 26
                await self.tick(100*(1-direction*.015))
                self.assertAlmostEqual(self.bot.positions[BTC]['amount'],.5)
                self.assertAlmostEqual(self.bot.exchange.positions[BTC]['margin'],5)
                self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'PARTIAL_SL')
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,market_data=self.data)
                await self.tick(100*(1-direction*.017))
                self.assertEqual(len(self.bot.exchange.trades),2)
                await self.tick(100*(1+direction*.004))
                self.assertEqual(self.bot.positions[BTC]["stop_kind"], "BE_AFTER_PARTIAL")
                await self.tick(100*(1+direction*.018))
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,market_data=self.data)
                self.assertEqual(self.bot.positions[BTC]['trail_locked'],.01)
                await self.tick(100*(1+direction*.01))
                self.assertFalse(self.bot.positions)
                self.assertFalse(self.bot.exchange.positions)
                trades=self.bot.exchange.trades
                self.assertAlmostEqual(sum(t['amount'] for t in trades if t['reduceOnly']),1)
                expected=100+sum(t['pnl_usdt']-t['fee_usdt'] for t in trades)
                self.assertAlmostEqual(self.bot.exchange.balance,expected)

    async def test_partial_then_tightened_stop(self):
        await self.bot.enter(BTC,'buy',100)
        self.bot.positions[BTC]['entry_time'] -= 26
        await self.tick(98.5)
        self.assertAlmostEqual(self.bot.positions[BTC]['sl'],98.2)
        await self.tick(98.2)
        self.assertFalse(self.bot.positions)
        self.assertAlmostEqual(self.bot.exchange.balance,98.35)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'POST_PARTIAL')

    async def test_gap_through_stop_closes_all(self):
        for side,price in [('buy',96),('sell',104)]:
            self.data.price=100
            self.bot=V2PaperHunter(self.path,fee_rate=0,reset=True,market_data=self.data)
            await self.bot.enter(BTC,side,100)
            await self.tick(price)
            self.assertFalse(self.bot.positions)
            self.assertEqual(len(self.bot.exchange.trades),2)
            self.assertAlmostEqual(self.bot.exchange.balance,96)

    async def test_fixed_sizing_independent_of_balance_and_entry_fill_anchor(self):
        self.bot.exchange.balance=200
        self.data.price=101
        await self.bot.enter(BTC,'buy',100)
        p=self.bot.positions[BTC]
        self.assertEqual(p['entry'],101)
        self.assertAlmostEqual(p['sl'],98.172)
        self.assertLessEqual(self.bot.exchange.positions[BTC]['margin'],10)
        self.assertGreater(self.bot.exchange.positions[BTC]['margin'],9.98)

    async def test_insufficient_funds_reject_entry(self):
        self.bot.exchange.balance=4
        self.assertFalse(await self.bot.enter(BTC,'buy',100))
        self.assertFalse(self.bot.exchange.positions)

    async def test_failed_partial_can_retry(self):
        await self.bot.enter(BTC,'buy',100)
        self.bot.positions[BTC]['entry_time'] -= 26
        with patch.object(self.bot.exchange,'create_order',side_effect=ConnectionError('offline')):
            await self.tick(98.5)
        self.assertFalse(self.bot.positions[BTC]['partial_done'])
        await self.tick(98.5)
        self.assertTrue(self.bot.positions[BTC]['partial_done'])
        self.assertAlmostEqual(self.bot.positions[BTC]['amount'],.5)

    async def test_fast_adverse_boundary_and_survival_persist(self):
        with patch.object(v2.time, "time", return_value=1000):
            await self.bot.enter(BTC, "buy", 100)
        with patch.object(v2.time, "time", return_value=1025):
            await self.tick(98.5)
            self.assertFalse(self.bot.positions)
            self.assertEqual(len(self.bot.exchange.trades), 2)
            self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "FAST_ADVERSE")
            self.assertFalse(await self.bot.enter(BTC, "buy", 100))
            self.bot.register_loss("ETH")
            self.bot.register_loss("SOL")
            self.bot.save()
            self.bot = V2PaperHunter(self.path, fee_rate=0, market_data=self.data)
            self.assertEqual(self.bot.last_loss[BTC], 1025)
            self.assertEqual(self.bot.consecutive_losses, 3)
            self.data.price = 100
            await self.bot.enter("XRP", "sell", 100)
            self.assertAlmostEqual(self.bot.exchange.positions["XRP"]["margin"], 5)

    async def test_drawdown_halves_margin_without_loss_streak(self):
        self.bot.exchange.balance = 95
        await self.bot.enter(BTC, "buy", 100)
        self.assertEqual(self.bot.exchange.positions[BTC]["margin"], 5)

    async def test_scanner_rejects_weak_otherwise_valid_signal(self):
        scanner = ContinuousScanner(self.bot, clock=lambda:100, wall_clock=lambda:NOW/1000)
        await scanner.refresh_universe()
        await scanner.seed(scanner.symbols)
        scanner.connected = True
        async def weak_book(*args, **kwargs):
            return {"bids": [[100, 7]], "asks": [[100, 3]]}
        self.data.fetch_order_book = weak_book
        scanner.ingest(event("BTCUSDT", close=101.3, volume=4))
        await scanner.evaluate(BTC)
        self.assertEqual(scanner.book_checks, 1)
        self.assertFalse(self.bot.positions)

    async def test_legacy_partial_resumes_with_tighter_stop_and_same_ledger(self):
        await self.bot.enter(BTC, "buy", 100)
        self.bot.positions[BTC]["entry_time"] -= 26
        await self.tick(98.5)
        state = json.loads(self.path.read_text())
        old = state["settings"]
        keys = ("mode", "position_basis", "stop_basis", "LEVERAGE", "FIXED_MARGIN_USDT",
                "CASCADE_THRESHOLD", "VOLUME_SPIKE", "COOLDOWN_SEC", "SCAN_INTERVAL",
                "MAX_SYMBOLS_TO_SCAN", "MAX_CONCURRENT_POSITIONS", "MIN_QUOTE_VOLUME")
        state["settings"] = {key: old[key] for key in keys}
        state["settings"].update(STOP_LOSS_PCT=.03, PARTIAL_LOSS_PCT=.015,
                                 TRAIL_START_PCT=.01, TRAIL_STEP_PCT=.005)
        state["strategy_positions"][BTC] = dict(
            side="buy", entry=100, amount=.5, tp=None, partial_closed=True, trail_level=0, sl=97
        )
        self.path.write_text(json.dumps(state))
        self.bot = V2PaperHunter(self.path, fee_rate=0, market_data=self.data)
        self.assertEqual(self.bot.exchange.trades, state["trades"])
        self.assertEqual(self.bot.positions[BTC]["amount"], .5)
        self.assertEqual(self.bot.positions[BTC]["stop_kind"], "POST_PARTIAL")
        await self.tick(98.2)
        self.assertEqual(self.bot.exchange.trades[-1]["exit_reason"], "POST_PARTIAL")
        self.assertFalse(self.bot.positions)

    async def test_incompatible_state_rejected_without_modifying_file(self):
        self.bot.save()
        state=json.loads(self.path.read_text())
        state['settings']['FIXED_MARGIN_USDT']=99
        self.path.write_text(json.dumps(state))
        before=self.path.read_bytes()
        with self.assertRaises(ValueError):V2PaperHunter(self.path,fee_rate=0,market_data=self.data)
        self.assertEqual(self.path.read_bytes(),before)

    async def test_scanner_enforces_six_positions_concurrently(self):
        self.data.tickers={f'COIN{i}/USDT:USDT':{'quoteVolume':25_000_000} for i in range(7)}
        scanner=ContinuousScanner(self.bot,config=v2,clock=lambda:100,wall_clock=lambda:NOW/1000)
        await scanner.refresh_universe()
        await scanner.seed(scanner.symbols)
        scanner.connected=True
        self.data.price=102
        for i in range(7):scanner.ingest(event(f'COIN{i}USDT'))
        with patch('continuous_scanner.BOOK_REQUEST_SPACING',0):
            await asyncio.gather(*(scanner.evaluate(s) for s in scanner.symbols))
        self.assertEqual(len(self.bot.positions),6)
