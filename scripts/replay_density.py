"""Run the same 100 deterministic paper signals against a source checkout."""

import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path


async def replay(source):
    sys.path.insert(0, str(source))
    from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter
    from continuous_scanner import ContinuousScanner

    # Use an identical public-data fixture for both checkouts.
    fixture_file = Path(__file__).resolve().parents[1] / "tests" / "fixtures.py"
    spec = importlib.util.spec_from_file_location("density_fixtures", fixture_file)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    stamp = 1800000000000

    class Data(fixtures.DynamicPublicData):
        async def fetch_ohlcv(self, symbol, timeframe, limit):
            return [[stamp - (29 - i) * 60000, 100, 100.15, 99.85, 100, 1] for i in range(30)]

        async def fetch_order_book(self, symbol, limit):
            pressure = (900, 100) if self.price > 100 else (100, 900)
            return {"bids": [[self.price - .01, pressure[0]]],
                    "asks": [[self.price + .01, pressure[1]]]}

    adaptive = hasattr(V2PaperHunter, "enter_signal")
    if adaptive:
        from execution.adaptive_runtime import CONFIG
        # Exercise the unconfirmed IOC path without wall-clock waits.
        CONFIG["confirm"]["confirm_timeout_sec"] = 0
        CONFIG["execution"]["entry_timeout_sec"] = 0

    entered = 0
    reasons = Counter()
    with tempfile.TemporaryDirectory() as temp:
        for i in range(100):
            symbol = "BTC/USDT:USDT" if i % 2 == 0 else "ETH/USDT:USDT"
            direction = 1 if i % 4 < 2 else -1
            close = 100 + direction * (1.3 + .01 * (i % 35))
            volume = 4 + (i % 7)
            data = Data()
            data.price = close
            bot = V2PaperHunter(Path(temp) / f"{i}.json", market_data=data)
            scanner = ContinuousScanner(bot, clock=lambda: 100, wall_clock=lambda: stamp / 1000)
            bot.scanner = scanner
            await scanner.refresh_universe()
            await scanner.seed(scanner.symbols)
            scanner.connected = True
            stream_id = scanner.stream_ids[symbol]
            scanner.ingest({"e": "kline", "E": stamp + 1, "s": stream_id, "st": 1, "k": {
                "s": stream_id, "i": "1m", "t": stamp, "o": 100, "h": max(100, close),
                "l": min(100, close), "c": close, "v": volume,
            }})
            await scanner.evaluate(symbol)
            entered += bool(bot.positions)
            for item in getattr(bot, "execution_events", ()):
                reasons[item["reason"]] += 1
    return {"signals": 100, "fills": entered, "events": dict(reasons), "adaptive": adaptive}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(replay(args.source.resolve())), sort_keys=True))
