from types import SimpleNamespace

import pytest

from alpha.micro_confirm import quality_gate
from execution.adaptive_runtime import CONFIG
from execution.entry_engine import plan_entry
from risk.exit_engine import ExitEngine, Position
from risk.sizing import position_size_from_risk
from risk.stop_engine import StopEngine, StopType, compute_liq_price


@pytest.mark.parametrize("side, expected", [("long", 98.4), ("short", 101.6)])
def test_liquidation_formula(side, expected):
    assert compute_liq_price(side, 100, 50, .004) == pytest.approx(expected)


@pytest.mark.parametrize("atr,swing,liq,kind,expected", [
    (1, 99.5, 95, StopType.STRUCTURAL, 99.45),
    (.4, 98, 95, StopType.ATR, 99.4),
    (1, 98, 98.4, StopType.LIQUIDATION, 99.876),
])
def test_closest_stop_and_safety_cap(atr, swing, liq, kind, expected):
    stop = StopEngine(CONFIG["risk"]).compute_initial_stop("long", 100, atr, swing, liq)
    assert stop.stop_type == kind
    assert stop.price == pytest.approx(expected)
    assert stop.distance_pct == pytest.approx(abs(100 - expected) / 100)


def test_risk_sizing():
    qty, notional, lev = position_size_from_risk(100, .008, .005, 120, 25)
    assert qty * .005 * 120 == pytest.approx(.8, abs=1e-6)
    assert notional == pytest.approx(qty * 120)
    assert lev == pytest.approx(notional / 100)


def test_time_stop():
    pos = Position("long", 100, 1, 0, 99, 103, "test")
    assert ExitEngine(CONFIG["exit"]).evaluate(pos, SimpleNamespace(price=100, now=1800, atr=1), {}) == "time_stop"


def test_breakeven_and_trail_only_tighten():
    pos = Position("short", 100, 1, 0, 101, 95, "test")
    engine = ExitEngine(CONFIG["exit"])
    assert engine.evaluate(pos, SimpleNamespace(price=99.69, now=10, atr=1), {}) is None
    assert pos.breakeven_armed
    assert pos.stop == pytest.approx(99.92)
    assert engine.evaluate(pos, SimpleNamespace(price=99.92, now=20, atr=1), {}) == "breakeven"


@pytest.mark.parametrize("features,context,expected", [
    ((.05, 6, 1), (1, 1), 1.0),
    ((0, 1, 0), (0, 0), 0.0),
    ((.02, 3.8, .6), (.5, .5), .3),
])
def test_quality_gate(features, context, expected):
    signal = SimpleNamespace(cascade_strength=features[0], volume_ratio=features[1], book_pressure=features[2])
    ctx = SimpleNamespace(regime_fit=context[0], cross_venue_agreement=context[1])
    assert quality_gate(signal, ctx, CONFIG["quality"]) == expected


def test_post_only_and_ioc_bound():
    book = SimpleNamespace(best_bid=99.99, best_ask=100.01, tick=.01)
    order = plan_entry("long", 100, 1, book, CONFIG["execution"])
    assert order["price"] <= book.best_bid
    assert order["fallback"]["price"] == pytest.approx(100.15)


def test_100_signal_execution_density():
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    reference = os.environ.get("KHALED_REFERENCE_SOURCE")
    if not reference:
        pytest.skip("Set KHALED_REFERENCE_SOURCE to the pinned Final-Edge v3 checkout")
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "replay_density.py"

    def fills(source):
        result = subprocess.run([sys.executable, str(script), "--source", str(source)],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    old, new = fills(reference), fills(root)
    assert not old["adaptive"] and new["adaptive"]
    assert old["signals"] == new["signals"] == 100
    assert old["fills"] > 0
    assert new["fills"] >= .9 * old["fills"]
