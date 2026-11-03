import json
import unittest
from pathlib import Path
import binance_cascade_hunter_dynamic_v2 as strategy


class BaselineTests(unittest.TestCase):
    def test_approved_settings_match_strategy(self):
        baseline=json.loads((Path(__file__).resolve().parents[1]/'baseline.json').read_text())
        self.assertEqual(baseline["version"], "3.0.0-FINAL-EDGE")
        self.assertIsNone(baseline["partial_loss_pct"])
        for key,name in {'leverage':'LEVERAGE','margin_fraction_per_position':'MAX_POSITION_PCT','max_concurrent_positions':'MAX_CONCURRENT_POSITIONS','stop_loss_pct':'HARD_STOP_PCT','trail_arm_pct':'TRAIL_ARM_PCT','trail_distance_pct':'TRAIL_DISTANCE_PCT','orderbook_imbalance_threshold':'ORDERBOOK_IMBALANCE','min_signal_strength':'MIN_SIGNAL_STRENGTH','same_symbol_loss_cooldown':'SAME_SYMBOL_LOSS_COOLDOWN','cascade_threshold':'CASCADE_THRESHOLD','volume_spike':'VOLUME_SPIKE','max_symbols':'MAX_SYMBOLS_TO_SCAN','universe_refresh_sec':'SCAN_INTERVAL','min_quote_volume_usdt':'MIN_QUOTE_VOLUME','cooldown_sec':'COOLDOWN_SEC'}.items():
            with self.subTest(key=key):self.assertEqual(baseline[key],getattr(strategy,name))

    def test_strategy_cannot_construct_real_exchange(self):
        with self.assertRaises(RuntimeError):strategy.CascadeHunter()
        self.assertFalse(hasattr(strategy,'API_KEY'))
        self.assertFalse(hasattr(strategy.CascadeHunter,'scan_and_trade'))

