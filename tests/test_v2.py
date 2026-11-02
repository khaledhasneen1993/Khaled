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
                for gain, stop in [(0.0099,-.03),(.01,.005),(.0149,.005),(.015,.01),(.027,.02),(.021,.02)]:
                    await self.tick(100 * (1 + direction * gain))
                    self.assertAlmostEqual(self.bot.positions[BTC]['sl'],100*(1+direction*stop))
                await self.tick(100 * (1 + direction * .02))
                self.assertFalse(self.bot.positions)
                self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'TRAILING_SL')
                self.assertAlmostEqual(self.bot.exchange.balance,102)

    async def test_partial_once_then_recovery_trailing_with_restore(self):
        for side,direction in [('buy',1),('sell',-1)]:
            with self.subTest(side=side):
                self.data.price=100
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,reset=True,market_data=self.data)
                await self.bot.enter(BTC,side,100)
                await self.tick(100*(1-direction*.015))
                self.assertAlmostEqual(self.bot.positions[BTC]['amount'],.5)
                self.assertAlmostEqual(self.bot.exchange.positions[BTC]['margin'],5)
                self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'PARTIAL_SL')
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,market_data=self.data)
                await self.tick(100*(1-direction*.02))
                self.assertEqual(len(self.bot.exchange.trades),2)
                await self.tick(100*(1+direction*.015))
                self.bot=V2PaperHunter(self.path,fee_rate=.0005,market_data=self.data)
                self.assertEqual(self.bot.positions[BTC]['trail_level'],2)
                await self.tick(100*(1+direction*.01))
                self.assertFalse(self.bot.positions)
                self.assertFalse(self.bot.exchange.positions)
                trades=self.bot.exchange.trades
                self.assertAlmostEqual(sum(t['amount'] for t in trades if t['reduceOnly']),1)
                expected=100+sum(t['pnl_usdt']-t['fee_usdt'] for t in trades)
                self.assertAlmostEqual(self.bot.exchange.balance,expected)

    async def test_partial_then_hard_stop(self):
        await self.bot.enter(BTC,'buy',100)
        await self.tick(98.5)
        await self.tick(97)
        self.assertFalse(self.bot.positions)
        self.assertAlmostEqual(self.bot.exchange.balance,97.75)
        self.assertEqual(self.bot.exchange.trades[-1]['exit_reason'],'SL')

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
        self.assertAlmostEqual(p['sl'],97.97)
        self.assertLessEqual(self.bot.exchange.positions[BTC]['margin'],10)
        self.assertGreater(self.bot.exchange.positions[BTC]['margin'],9.98)

    async def test_insufficient_funds_reject_entry(self):
        self.bot.exchange.balance=9
        self.assertFalse(await self.bot.enter(BTC,'buy',100))
        self.assertFalse(self.bot.exchange.positions)

    async def test_failed_partial_can_retry(self):
        await self.bot.enter(BTC,'buy',100)
        with patch.object(self.bot.exchange,'create_order',side_effect=ConnectionError('offline')):
            await self.tick(98.5)
        self.assertFalse(self.bot.positions[BTC]['partial_closed'])
        await self.tick(98.5)
        self.assertTrue(self.bot.positions[BTC]['partial_closed'])
        self.assertAlmostEqual(self.bot.positions[BTC]['amount'],.5)

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
