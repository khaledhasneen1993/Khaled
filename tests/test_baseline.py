import json
import unittest
from pathlib import Path
import binance_cascade_hunter_dynamic_v2 as strategy


class BaselineTests(unittest.TestCase):
    def test_approved_settings_match_strategy(self):
        baseline=json.loads((Path(__file__).resolve().parents[1]/'baseline.json').read_text())
        self.assertEqual(baseline["trail_levels"], [list(level) for level in strategy.TRAIL_LEVELS])
        self.assertEqual(baseline["trail_start_pct"], strategy.TRAIL_LEVELS[0][0])
        for key,name in {'leverage':'LEVERAGE','fixed_margin_usdt':'FIXED_MARGIN_USDT','max_concurrent_positions':'MAX_CONCURRENT_POSITIONS','stop_loss_pct':'HARD_STOP_PCT','partial_loss_pct':'PARTIAL_LEVEL','cascade_threshold':'CASCADE_THRESHOLD','volume_spike':'VOLUME_SPIKE','max_symbols':'MAX_SYMBOLS_TO_SCAN','universe_refresh_sec':'SCAN_INTERVAL','min_quote_volume_usdt':'MIN_QUOTE_VOLUME','cooldown_sec':'COOLDOWN_SEC'}.items():
            with self.subTest(key=key):self.assertEqual(baseline[key],getattr(strategy,name))

    def test_strategy_cannot_construct_real_exchange(self):
        with self.assertRaises(RuntimeError):strategy.CascadeHunter()
        self.assertFalse(hasattr(strategy,'API_KEY'))
        self.assertFalse(hasattr(strategy.CascadeHunter,'scan_and_trade'))

