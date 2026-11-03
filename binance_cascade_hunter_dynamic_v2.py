# language: Python 3.11+, file: binance_cascade_hunter_dynamic_v2.py
# target: paper USDT-M with path-aware adaptive exits
# deps: pip install ccxt pandas numpy aiohttp

import asyncio
import time
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("CASCADE")

# === CONFIG ===

LEVERAGE = 10
FIXED_MARGIN_USDT = 10.0        # fixed margin per entry
CASCADE_THRESHOLD = 0.012        # 1.2%
VOLUME_SPIKE = 3.5
HARD_STOP_PCT = 0.028
FAST_ADVERSE_PCT = 0.010
PARTIAL_LEVEL = 0.015
PARTIAL_FRAC = 0.50
POST_PARTIAL_STOP_FROM_ENTRY = 0.018
POST_PARTIAL_STOP_FROM_PRICE = 0.006
BREAKEVEN_ARM_PCT = 0.004
BREAKEVEN_LOCK_PCT = 0.0005
TRAIL_LEVELS = [
    (0.008, 0.000),
    (0.012, 0.005),
    (0.018, 0.010),
    (0.025, 0.015),
    (0.035, 0.022),
    (0.050, 0.030),
]
MIN_SIGNAL_STRENGTH = 0.025
SAME_SYMBOL_LOSS_COOLDOWN = 900
MAX_CONSECUTIVE_LOSSES = 3
SURVIVAL_DRAWDOWN = 0.05
SURVIVAL_MARGIN_MULT = 0.50
FAST_WINDOW_SEC = 25
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
        self.equity_peak = max(self.equity_peak, self.exchange.equity)
        survival = (self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES
                    or self.exchange.equity <= self.equity_peak * (1 - SURVIVAL_DRAWDOWN))
        margin = FIXED_MARGIN_USDT * (SURVIVAL_MARGIN_MULT if survival else 1)
        notional = margin * LEVERAGE
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
                "partial_done": False, "trail_locked": 0.0, "entry_time": time.time(),
                "max_adverse": 0.0, "max_favorable": 0.0, "fast_checked": False,
                "net_pnl": -order["fee_usdt"],
            }
            log.info(f"ENTER {side.upper()} {symbol} size={amount:.4f} @ {price:.4f}")
            return True
        except Exception as e:
            log.error(f"Entry failed {symbol}: {e}")
            return False

    def tighten_stop(self, p, candidate, kind):
        direction = 1 if p["side"] == "buy" else -1
        if direction * (candidate - p["sl"]) > 0:
            p["sl"] = candidate
            p["stop_kind"] = kind

    def register_loss(self, symbol):
        self.last_loss[symbol] = time.time()
        self.consecutive_losses += 1

    def register_win(self):
        self.consecutive_losses = 0

    async def partial_close(self, position_id, fraction, price, reason):
        p = self.positions[position_id]
        try:
            amount = float(self.exchange.amount_to_precision(position_id, p["amount"] * fraction))
            if not 0 < amount < p["amount"]:
                return False
            side = "sell" if p["side"] == "buy" else "buy"
            fill = await self.exchange.create_order(
                position_id, "market", side, amount, params={"reduceOnly": True}
            )
        except Exception as error:
            log.warning("Partial close failed %s: %s", position_id, error)
            return False
        p["amount"] = self.exchange.positions[position_id]["amount"]
        p["net_pnl"] += fill["pnl_usdt"] - fill["fee_usdt"]
        self.record_exit_reason(position_id, reason, price)
        return True

    async def close_position(self, symbol, p, price, reason):
        side = "sell" if p["side"] == "buy" else "buy"
        fill = await self.exchange.create_order(
            symbol, "market", side, p["amount"], params={"reduceOnly": True}
        )
        self.record_exit_reason(symbol, reason, price)
        del self.positions[symbol]
        net = p["net_pnl"] + fill["pnl_usdt"] - fill["fee_usdt"]
        if net > 0:
            self.register_win()
        elif net < 0 or reason in ("FAST_ADVERSE", "SL"):
            self.register_loss(symbol)
        log.info("EXIT %s %s @ %.8g | net=%.6f", reason, symbol, price, net)

    async def manage_exits(self):
        for symbol, p in list(self.positions.items()):
            try:
                price = float((await self.exchange.fetch_ticker(symbol))["last"])
                self.equity_peak = max(self.equity_peak, self.exchange.equity)
                entry = p["original_entry"]
                direction = 1 if p["side"] == "buy" else -1
                move = direction * (price / entry - 1)
                adverse = max(0.0, -move)
                p["max_adverse"] = max(p["max_adverse"], adverse)
                p["max_favorable"] = max(p["max_favorable"], move)
                eps = 1e-12
                if direction * (price - p["sl"]) <= entry * eps:
                    await self.close_position(symbol, p, price, p["stop_kind"])
                    continue

                age = time.time() - p["entry_time"]
                if age <= FAST_WINDOW_SEC and adverse + eps >= FAST_ADVERSE_PCT and not p["partial_done"]:
                    await self.close_position(symbol, p, price, "FAST_ADVERSE")
                    continue
                if age > FAST_WINDOW_SEC:
                    p["fast_checked"] = True
                    if not p["partial_done"] and adverse + eps >= PARTIAL_LEVEL:
                        if await self.partial_close(symbol, PARTIAL_FRAC, price, "PARTIAL_SL"):
                            p["partial_done"] = True
                            entry_stop = entry * (1 - direction * POST_PARTIAL_STOP_FROM_ENTRY)
                            price_stop = price * (1 - direction * POST_PARTIAL_STOP_FROM_PRICE)
                            candidate = max(entry_stop, price_stop) if direction == 1 else min(entry_stop, price_stop)
                            self.tighten_stop(p, candidate, "POST_PARTIAL")
                            # A gap can leave the new stop already crossed.
                            if direction * (price - p["sl"]) <= entry * eps:
                                await self.close_position(symbol, p, price, p["stop_kind"])
                                continue

                if p["partial_done"] and move + eps >= BREAKEVEN_ARM_PCT:
                    self.tighten_stop(p, entry * (1 + direction * BREAKEVEN_LOCK_PCT), "BE_AFTER_PARTIAL")
                for trigger, locked in TRAIL_LEVELS:
                    if move + eps < trigger:
                        break
                    self.tighten_stop(p, entry * (1 + direction * locked), "TRAILING_SL")
                    p["trail_locked"] = max(p["trail_locked"], locked)
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

