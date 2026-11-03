# language: Python 3.11+, file: binance_cascade_hunter_dynamic_v2.py
# target: paper USDT-M with adaptive risk and execution
# deps: pip install ccxt pandas numpy aiohttp

import asyncio
import time
import pandas as pd
import logging
from execution.adaptive_runtime import AdaptiveExecution, CONFIG
from execution.market_view import candle_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("CASCADE")

# === CONFIG ===

LEVERAGE = 50
MAX_POSITION_PCT = 0.10
CASCADE_THRESHOLD = 0.012
VOLUME_SPIKE = 3.5
ORDERBOOK_IMBALANCE = 0.35
COOLDOWN_SEC = 8
SCAN_INTERVAL = 600
MAX_SYMBOLS_TO_SCAN = 40
MAX_CONCURRENT_POSITIONS = 4
MIN_QUOTE_VOLUME = 15_000_000

HARD_STOP_PCT = 0.08
TRAIL_ARM_PCT = 0.015
TRAIL_DISTANCE_PCT = 0.006
MIN_SIGNAL_STRENGTH = 0.020
SAME_SYMBOL_LOSS_COOLDOWN = 600


class CascadeHunter(AdaptiveExecution):
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

            long_sig = ret_1m > CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb > ORDERBOOK_IMBALANCE
            short_sig = ret_1m < -CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb < -ORDERBOOK_IMBALANCE

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
        signal = await self.detect_cascade(symbol)
        if signal is None or not (signal["long"] or signal["short"]):
            return 0.0
        side = "long" if signal["long"] else "short"
        rows = await self.exchange.fetch_ohlcv(symbol, "1m", limit=30)
        atr, swing, _ = candle_context(rows, CONFIG["adaptive"]["atr_period"], side)
        price = float((await self.exchange.fetch_ticker(symbol))["last"])
        qty, _, _, _ = self.risk_plan(side, price, atr, swing, 1.0)
        return float(self.exchange.amount_to_precision(symbol, qty))

    async def enter(self, symbol, side, price):
        signal = await self.detect_cascade(symbol)
        if signal is None or not signal.get("long" if side == "buy" else "short"):
            return False
        return await self.enter_signal(symbol, {
            "side": side, "price": float(price), "ret": float(signal["ret"]),
            "spike": float(signal["vol_spike"]), "imbalance": float(signal["imb"]),
        })

    async def close_position(self, symbol, p, price):
        await self.finalize_position(symbol, p, price, time.time(), p["stop_kind"])

    async def manage_exits(self):
        await self.adaptive_exits()

    async def exit_watcher(self):
        """مراقبة الخروج بشكل مستمر بين المسحات"""
        while True:
            try:
                async with self.lock:
                    await self.manage_exits()
                await asyncio.sleep(self.exit_interval_sec)
            except Exception as e:
                log.error(f"Exit watcher error: {e}")
                await asyncio.sleep(15)


if __name__ == "__main__":
    raise SystemExit("Strategy module only. Run: python -u run_paper_live.py")

