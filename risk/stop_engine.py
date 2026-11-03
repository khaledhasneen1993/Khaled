from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Optional


class StopType(Enum):
    STRUCTURAL = "structural"
    ATR = "atr"
    LIQUIDATION = "liquidation"


@dataclass
class StopPlan:
    price: float
    stop_type: StopType
    reason: str
    distance_pct: float


def _side_mult(side: str) -> int:
    if side not in {"long", "short"}:
        raise ValueError("Expected long or short")
    return 1 if side == "long" else -1


class StopEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def compute_initial_stop(self, side: str, entry: float, atr: float,
                             structure_level: Optional[float], liq_price: float) -> StopPlan:
        sm = _side_mult(side)
        if not all(isfinite(x) and x > 0 for x in (entry, liq_price)):
            raise ValueError("Invalid entry or liquidation price")
        cap = liq_price * (1 + sm * self.cfg["liq_safety_margin"])
        if sm * (entry - cap) <= 0:
            raise ValueError("Liquidation safety buffer leaves no room for a stop")
        cap_distance = abs(entry - cap) / entry
        candidates = [StopPlan(cap, StopType.LIQUIDATION, "pre-liquidation safety", cap_distance)]
        if structure_level is not None and sm * (entry - structure_level) > 0:
            price = structure_level - sm * entry * self.cfg["struct_buffer"]
            candidates.append(StopPlan(price, StopType.STRUCTURAL, "local swing break",
                                       abs(entry - price) / entry))
        if isfinite(atr) and atr > 0:
            price = entry - sm * atr * self.cfg["atr_mult"]
            candidates.append(StopPlan(price, StopType.ATR, f"ATR x{self.cfg['atr_mult']}",
                                       abs(entry - price) / entry))
        chosen = min(candidates, key=lambda plan: plan.distance_pct)
        floor = self.cfg["min_stop_pct"]
        if chosen.distance_pct < floor:
            if floor >= cap_distance:
                return candidates[0]
            return StopPlan(entry * (1 - sm * floor), chosen.stop_type,
                            chosen.reason + " (min floor)", floor)
        return chosen


def compute_liq_price(side: str, entry: float, leverage: float, mmr: float) -> float:
    return entry * (1 - _side_mult(side) * (1 / leverage - mmr))
