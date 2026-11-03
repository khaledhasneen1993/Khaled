from dataclasses import dataclass
from typing import Optional

from risk.stop_engine import _side_mult


@dataclass
class Position:
    side: str
    entry: float
    qty: float
    opened_at: float
    stop: float
    tp: float
    signal_id: str
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    breakeven_armed: bool = False
    bars_open: int = 0
    stop_reason: str = "stop_loss"


class ExitEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def evaluate(self, pos: Position, market, signal_ctx: dict) -> Optional[str]:
        sm = _side_mult(pos.side)
        pnl = sm * (market.price - pos.entry) / pos.entry
        pos.max_favorable = max(pos.max_favorable, pnl)
        age = market.now - pos.opened_at
        pos.max_adverse = min(pos.max_adverse, pnl)
        if sm * (market.price - pos.stop) <= 0:
            return pos.stop_reason
        if sm * (pos.tp - market.price) <= 0:
            return "take_profit"
        if not pos.breakeven_armed and pnl >= self.cfg["be_trigger_pct"]:
            candidate = pos.entry * (1 + sm * self.cfg["be_offset_pct"])
            if sm * (candidate - pos.stop) > 0:
                pos.stop, pos.stop_reason = candidate, "breakeven"
            pos.breakeven_armed = True
        if pos.breakeven_armed and self.cfg["trail_enabled"] and market.atr > 0:
            candidate = market.price - sm * market.atr * self.cfg["trail_atr_mult"]
            if sm * (candidate - pos.stop) > 0:
                pos.stop, pos.stop_reason = candidate, "trailing_stop"
        if age >= 60 * self.cfg["time_stop_min"] and pnl < self.cfg["time_stop_min_profit"]:
            return "time_stop"
        if signal_ctx.get("signal_alive") is False:
            return "signal_decay"
        if (age <= self.cfg.get("adverse_excursion_window_sec", float("inf"))
                and pos.max_adverse < -self.cfg["adverse_excursion_pct"]):
            return "adverse_excursion"
        if pos.bars_open >= self.cfg["max_bars_in_trade"]:
            return "max_bars"
        return None
