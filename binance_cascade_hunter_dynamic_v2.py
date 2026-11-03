# language: Python 3.11+, file: binance_cascade_hunter_dynamic_v2.py
# target: paper USDT-M with Final-Edge v3 exits
# deps: pip install ccxt pandas numpy aiohttp

import asyncio
import time
import pandas as pd
import logging

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
        price = (await self.exchange.fetch_ticker(symbol))["last"]
        balance = (await self.exchange.fetch_balance())["USDT"]["total"]
        notional = max(0.0, balance) * MAX_POSITION_PCT * LEVERAGE
        amount = notional / price
        amount = float(self.exchange.amount_to_precision(symbol, amount))
        return amount

    async def enter(self, symbol, side, price):
        now = time.time()
        if symbol in self.positions or now - self.last_loss.get(symbol, -float("inf")) < SAME_SYMBOL_LOSS_COOLDOWN:
            return False
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
            direction = 1 if side == "buy" else -1
            self.positions[symbol] = {
                "side": side, "entry": price, "original_entry": price, "amount": amount,
                "sl": price * (1 - direction * HARD_STOP_PCT), "stop_kind": "SL",
                "trailing": False, "best_price": price, "entry_time": time.time(),
                "entry_fee": order["fee_usdt"],
            }
            log.info(f"ENTER {side.upper()} {symbol} size={amount:.4f} @ {price:.4f}")
            return True
        except Exception as e:
            log.error(f"Entry failed {symbol}: {e}")
            return False

    async def close_position(self, symbol, p, price):
        side = "sell" if p["side"] == "buy" else "buy"
        fill = await self.exchange.create_order(
            symbol, "market", side, p["amount"], params={"reduceOnly": True}
        )
        self.record_exit_reason(symbol, p["stop_kind"], price)
        del self.positions[symbol]
        net = fill["pnl_usdt"] - fill["fee_usdt"] - p["entry_fee"]
        if net < 0:
            self.last_loss[symbol] = time.time()
        log.info("EXIT %s %s @ %.8g | net=%.6f", p["stop_kind"], symbol, price, net)

    async def manage_exits(self):
        for symbol, p in list(self.positions.items()):
            try:
                price = float((await self.exchange.fetch_ticker(symbol))["last"])
                direction = 1 if p["side"] == "buy" else -1
                if direction * (price - p["sl"]) <= p["entry"] * 1e-12:
                    await self.close_position(symbol, p, price)
                    continue

                move = direction * (price / p["entry"] - 1)
                if not p["trailing"]:
                    if move + 1e-12 < TRAIL_ARM_PCT:
                        continue
                    p["trailing"] = True
                    p["best_price"] = price
                elif direction * (price - p["best_price"]) > 0:
                    p["best_price"] = price

                candidate = p["best_price"] * (1 - direction * TRAIL_DISTANCE_PCT)
                if direction * (candidate - p["sl"]) > 0:
                    p["sl"] = candidate
                    p["stop_kind"] = "TRAILING_SL"
            except Exception as error:
                log.error("Exit check failed %s: %s", symbol, error)

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

