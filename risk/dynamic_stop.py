"""Monotone stops only. Closing remains the responsibility of manage_exits."""
import math
from .liq_math import direction


def update_dynamic_stop(pos, market, cfg: dict) -> None:
    sm = direction(pos.side)
    if not math.isfinite(market.price) or market.price <= 0:
        raise ValueError("Invalid stop-management price")
    pnl_pct = (market.price - pos.entry) / pos.entry * sm
    # Trigger detection tolerates price multiplication rounding only.
    if not pos.breakeven_armed and (pnl_pct >= cfg["be_trigger_pct"]
            or math.isclose(pnl_pct, cfg["be_trigger_pct"], rel_tol=0, abs_tol=math.ulp(market.price) / pos.entry)):
        candidate = pos.entry * (1 + sm * cfg["be_offset_pct"])
        if sm * (candidate - pos.stop) > 0:
            pos.stop = candidate
        pos.breakeven_armed = True
    if pos.breakeven_armed and cfg["trail_enabled"] and math.isfinite(market.atr) and market.atr > 0:
        candidate = market.price - sm * market.atr * cfg["trail_atr_mult"]
        if sm * (candidate - pos.stop) > 0:
            pos.stop = candidate
