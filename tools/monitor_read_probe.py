"""Repeat monitor account/order reads without starting or resetting trading."""
import asyncio
import json
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.demo_preflight import ReadOnlyDemo, demo_config
from infra.rate_limit import TokenBucket
from infra.errors import diagnostic_fields


async def probe(exchange, checks, interval, emit):
    failures = 0
    for index in range(checks):
        stage = 'account_refresh'
        try:
            await exchange.refresh_account()
            stage = 'account_values'
            if not all(math.isfinite(v) for v in (exchange.balance, exchange.equity, exchange.free, exchange.used)):
                raise ValueError('Nonfinite account value')
            stage = 'open_orders_snapshot'
            orders = await exchange.get_open_orders()
            emit({'check': index + 1, 'stage': 'rest_snapshot', 'ok': True,
                  'positions': len(exchange.positions),
                  'regular_orders': sum(not o.get('is_algo') for o in orders),
                  'algo_orders': sum(bool(o.get('is_algo')) for o in orders)})
        except Exception as error:
            failures += 1
            emit({'check': index + 1, 'stage': stage, 'ok': False, **diagnostic_fields(error)})
        if index + 1 < checks: await asyncio.sleep(interval)
    return failures


def main():
    async def run():
        cfg = demo_config()
        ec = cfg['exchange']
        ex = ReadOnlyDemo(cfg, TokenBucket(ec['rate_per_sec'], ec['rate_burst']))
        try:
            await ex.load_markets()
            failures = await probe(ex, ec['monitor_probe_checks'], ec['account_refresh_sec'],
                lambda record: print(json.dumps(record, allow_nan=False), flush=True))
            print(json.dumps({'probe_complete': True, 'failed_checks': failures,
                'read_only': True, 'kill_reset': False,
                'scope': 'REST account and order reads; not the complete trading monitor'}))
            return 1 if failures else 0
        finally: await ex.close()
    try: code = asyncio.run(run())
    except Exception as error:
        print(json.dumps({'stage': 'probe_initialization', 'ok': False, **diagnostic_fields(error)}))
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == '__main__': main()
