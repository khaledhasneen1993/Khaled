# language: Python 3.11+, file: binance_cascade_hunter_dynamic_v2.py
# target: paper USDT-M — $10 margin, 10x, 3% stop, half at -1.5%, step trailing, max 6
# deps: pip install ccxt pandas numpy aiohttp

import asyncio
import math
import pandas as pd
from datetime import datetime, timezone
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("CASCADE")

# === CONFIG ===

LEVERAGE = 10
FIXED_MARGIN_USDT = 10.0        # fixed margin per entry
CASCADE_THRESHOLD = 0.012        # 1.2%
VOLUME_SPIKE = 3.5
TRAIL_START_PCT = 0.01
TRAIL_STEP_PCT = 0.005
PARTIAL_LOSS_PCT = 0.015
STOP_LOSS_PCT = 0.03            # price movement from actual entry fill
COOLDOWN_SEC = 8

SCAN_INTERVAL = 600              # 10 دقائق
MAX_SYMBOLS_TO_SCAN = 40         # أعلى 40 عملة سيولة
MAX_CONCURRENT_POSITIONS = 6     # أقصى عدد مراكز مفتوحة في نفس الوقت
MIN_QUOTE_VOLUME = 15_000_000    # حجم تداول 24 ساعة أدنى (USDT)

class CascadeHunter:
    """V2 strategy methods; execution is supplied by the virtual account runtime."""

    def __init__(self):
        raise RuntimeError("Use V2PaperHunter or run_paper_live.py for the paper account")

    async def set_leverage(self, symbol):
        try:
            await self.exchange.set_leverage(LEVERAGE, symbol)
        except Exception as e:
            log.warning(f"Leverage set failed {symbol}: {e}")

    async def get_top_symbols(self):
        """يجيب أعلى العملات سيولة (USDT-M linear)"""
        try:
            tickers = await self.exchange.fetch_tickers()
            candidates = []
            for symbol, t in tickers.items():
                if not symbol.endswith(":USDT"):
                    continue
                if "/USDT:USDT" not in symbol:
                    continue
                quote_vol = float(t.get("quoteVolume") or 0)
                if quote_vol < MIN_QUOTE_VOLUME:
                    continue
                candidates.append((symbol, quote_vol))

            candidates.sort(key=lambda x: x[1], reverse=True)
            top = [s for s, _ in candidates[:MAX_SYMBOLS_TO_SCAN]]
            log.info(f"Scan universe: {len(top)} symbols")
            return top
        except Exception as e:
            log.error(f"Failed to fetch top symbols: {e}")
            return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    async def get_orderbook_imbalance(self, symbol, depth=20):
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=depth)
            bid_vol = sum(x[1] for x in ob["bids"][:depth])
            ask_vol = sum(x[1] for x in ob["asks"][:depth])
            if bid_vol + ask_vol == 0:
                return 0.0
            return (bid_vol - ask_vol) / (bid_vol + ask_vol)
        except Exception:
            return 0.0

    async def detect_cascade(self, symbol):
        try:
            ohlcv_1m = await self.exchange.fetch_ohlcv(symbol, "1m", limit=30)
            df1 = pd.DataFrame(ohlcv_1m, columns=["ts", "o", "h", "l", "c", "v"])

            ret_1m = (df1["c"].iloc[-1] - df1["c"].iloc[-3]) / df1["c"].iloc[-3]
            vol_avg = df1["v"].iloc[-15:-3].mean()
            vol_now = df1["v"].iloc[-1]
            vol_spike = vol_now / vol_avg if vol_avg > 0 else 0

            imb = await self.get_orderbook_imbalance(symbol)

            long_sig = ret_1m > CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb > 0.35
            short_sig = ret_1m < -CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb < -0.35

            strength = abs(ret_1m) * vol_spike * abs(imb)

            return {
                "long": long_sig,
                "short": short_sig,
                "ret": ret_1m,
                "vol_spike": vol_spike,
                "imb": imb,
                "price": float(df1["c"].iloc[-1]),
                "strength": strength,
                "symbol": symbol
            }
        except Exception as e:
            log.debug(f"Detect failed {symbol}: {e}")
            return None

    async def size_position(self, symbol):
        price = (await self.exchange.fetch_ticker(symbol))["last"]
        notional = FIXED_MARGIN_USDT * LEVERAGE
        amount = notional / price
        amount = float(self.exchange.amount_to_precision(symbol, amount))
        return amount

    async def enter(self, symbol, side, price):
        now = datetime.now(timezone.utc).timestamp()
        if symbol in self.last_trade and now - self.last_trade[symbol] < COOLDOWN_SEC:
            return False
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            return False

        amount = await self.size_position(symbol)
        if amount <= 0:
            return False

        try:
            await self.set_leverage(symbol)
            order = await self.exchange.create_order(
                symbol, "market", side, amount,
                params={"reduceOnly": False}
            )
            price = float(order["average"])
            amount = float(order["filled"])
            self.last_trade[symbol] = now
            self.positions[symbol] = {
                "side": side,
                "entry": price,
                "amount": amount,
                "tp": None,  # no fixed profit exit
                "partial_closed": False,
                "trail_level": 0,
                "sl": price * (1 - STOP_LOSS_PCT) if side == "buy" else price * (1 + STOP_LOSS_PCT)
            }
            log.info(f"ENTER {side.upper()} {symbol} size={amount:.4f} @ {price:.4f}")
            return True
        except Exception as e:
            log.error(f"Entry failed {symbol}: {e}")
            return False

    async def manage_exits(self):
        to_remove = []
        for symbol, pos in list(self.positions.items()):
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                price = ticker["last"]

                direction = 1 if pos["side"] == "buy" else -1
                gain = direction * (price / pos["entry"] - 1)
                close_side = "sell" if direction == 1 else "buy"
                # Only advance on complete 0.5% steps; the stop never retreats.
                if gain + 1e-12 >= TRAIL_START_PCT:
                    level = math.floor((gain + 1e-12) / TRAIL_STEP_PCT) - 1
                    if level > pos["trail_level"]:
                        pos["trail_level"] = level
                        pos["sl"] = pos["entry"] * (1 + direction * level * TRAIL_STEP_PCT)

                hit_sl = direction * (price - pos["sl"]) <= 1e-12 * pos["entry"]
                if hit_sl:
                    # A gap straight through -3% closes all remaining size.
                    await self.exchange.create_order(
                        symbol, "market", close_side, pos["amount"],
                        params={"reduceOnly": True}
                    )
                    reason = "TRAILING_SL" if pos["trail_level"] else "SL"
                    if hasattr(self, "record_exit_reason"):
                        self.record_exit_reason(symbol, reason, price)
                    log.info(f"EXIT {reason} {symbol} @ {price:.4f}")
                    to_remove.append(symbol)
                elif gain <= -PARTIAL_LOSS_PCT + 1e-12 and not pos["partial_closed"]:
                    half = float(self.exchange.amount_to_precision(symbol, pos["amount"] / 2))
                    if half <= 0 or half >= pos["amount"]:
                        raise ValueError("Half position cannot be represented at market precision")
                    order = await self.exchange.create_order(
                        symbol, "market", close_side, half, params={"reduceOnly": True}
                    )
                    pos["amount"] -= float(order["filled"])
                    pos["partial_closed"] = True
                    if hasattr(self, "record_exit_reason"):
                        self.record_exit_reason(symbol, "PARTIAL_SL", price)
                    log.info(f"PARTIAL_SL {symbol} @ {price:.4f}; remaining={pos['amount']}")
            except Exception as e:
                log.error(f"Exit check failed {symbol}: {e}")

        for s in to_remove:
            del self.positions[s]


    async def exit_watcher(self):
        """مراقبة الخروج بشكل مستمر بين المسحات"""
        while True:
            try:
                async with self.lock:
                    await self.manage_exits()
                await asyncio.sleep(8)
            except Exception as e:
                log.error(f"Exit watcher error: {e}")
                await asyncio.sleep(15)


if __name__ == "__main__":
    raise SystemExit("Strategy module only. Run: python -u run_paper_live.py")

