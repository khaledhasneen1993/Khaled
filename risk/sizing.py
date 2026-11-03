from math import isfinite


def position_size_from_risk(equity: float, risk_pct: float, stop_distance_pct: float,
                            price: float, max_implied_leverage: float) -> tuple[float, float, float]:
    values = (equity, risk_pct, stop_distance_pct, price, max_implied_leverage)
    if not all(isfinite(value) and value > 0 for value in values):
        raise ValueError("Risk sizing inputs must be finite and positive")
    notional = min(equity * risk_pct / stop_distance_pct, equity * max_implied_leverage)
    return notional / price, notional, notional / equity
