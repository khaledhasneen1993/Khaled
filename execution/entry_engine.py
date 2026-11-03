from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from risk.stop_engine import _side_mult


def round_to_tick(price, tick, side):
    step = Decimal(str(tick))
    rounding = ROUND_FLOOR if side == "long" else ROUND_CEILING
    return float((Decimal(str(price)) / step).to_integral_value(rounding=rounding) * step)


def plan_entry(side: str, ref_price: float, atr: float, book, cfg: dict) -> dict:
    sm = _side_mult(side)
    retrace = atr * cfg["retrace_atr"]
    if sm == 1:
        limit = min(book.best_bid, max(ref_price - retrace, book.best_bid - cfg["min_tick_offset"]))
    else:
        limit = max(book.best_ask, min(ref_price + retrace, book.best_ask + cfg["min_tick_offset"]))
    return {
        "type": "LIMIT", "tif": "GTX", "price": round_to_tick(limit, book.tick, side),
        "timeout_sec": cfg["entry_timeout_sec"],
        "fallback": {
            "type": "IOC", "price": ref_price * (1 + sm * cfg["max_chase_pct"]),
            "qty_pct": cfg["fallback_qty_pct"],
        },
    }


def execution_guard(order: dict, market, cfg: dict) -> str | None:
    if market.spread() > cfg["max_spread"]:
        return "spread_spike"
    if market.expected_slippage(order["qty"]) > cfg["max_slippage_bps"]:
        return "depth_thin"
    if market.latency_ms() > cfg["max_latency_ms"]:
        return "latency_spike"
    if abs(market.price - order["ref_price"]) / order["ref_price"] > cfg["max_chase_pct"]:
        return "price_moved"
    return None
