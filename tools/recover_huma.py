"""Recover the two historical HUMA orders using GET only; preview before apply."""
import argparse
import asyncio
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.demo_preflight import ReadOnlyDemo, demo_config
from infra.rate_limit import TokenBucket
from journal.wal import WriteAheadLog
from infra.journal import atomic_json


async def recover(exchange, history=None):
    cfg = {**exchange.cfg['demo'], **(history or {})}
    if any(cfg.get(key) is None for key in ('huma_entry_order_id', 'huma_close_order_id', 'huma_confirmed_realized_pnl')):
        raise ValueError('Provide historical order IDs and expected PnL using --history-config locally')
    symbol = cfg['huma_symbol']
    result = {'symbol': symbol, 'mode': 'testnet', 'recovered': True, 'source': 'Binance',
        'historical_atr': None, 'historical_tp': None, 'historical_sl': None,
        'historical_leverage': None, 'historical_margin': None, 'protection_status': 'unknown',
        'exit_reason': 'historical_recovery_unknown', 'orders': []}
    for role, order_id, side in [('entry', cfg['huma_entry_order_id'], 'BUY'), ('exit', cfg['huma_close_order_id'], 'SELL')]:
        order = await exchange._request('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id}, retry=True)
        fills = await exchange._request('GET', '/fapi/v1/userTrades', {'symbol': symbol, 'orderId': order_id,
            'limit': exchange.cfg['exchange']['trade_page_limit']}, retry=True)
        if (order['status'] != 'FILLED' or order['side'] != side or order['symbol'] != symbol
                or int(order['orderId']) != order_id or not fills): raise ValueError('Historical order not fully verifiable')
        reducing = order.get('reduceOnly') in (True, 'true')
        if reducing != (role == 'exit') or Decimal(order['executedQty']) <= 0:
            raise ValueError('Historical order direction/reduction mismatch')
        if any(f['commissionAsset'] != 'USDT' for f in fills): raise ValueError('Non-USDT accounting needs review')
        qty = sum((Decimal(f['qty']) for f in fills), Decimal(0))
        if qty != Decimal(order['executedQty']): raise ValueError('Incomplete historical executions')
        result['orders'].append({'role': role, 'id': str(order_id), 'client_id': order.get('clientOrderId'),
            'side': side, 'quantity': str(qty), 'average': order['avgPrice'],
            'time_ms': order.get('updateTime', order.get('time')), 'reduceOnly': order['reduceOnly'],
            'realized_pnl': str(sum((Decimal(f['realizedPnl']) for f in fills), Decimal(0))),
            'commission': str(sum((Decimal(f['commission']) for f in fills), Decimal(0)))})
    if Decimal(result['orders'][0]['quantity']) != Decimal(result['orders'][1]['quantity']): raise ValueError('Historical entry/exit mismatch')
    result['realized_pnl'] = str(sum((Decimal(o['realized_pnl']) for o in result['orders']), Decimal(0)))
    result['commission'] = str(sum((Decimal(o['commission']) for o in result['orders']), Decimal(0)))
    if Decimal(result['realized_pnl']) != Decimal(cfg['huma_confirmed_realized_pnl']):
        raise ValueError('Binance data conflicts with confirmed historical PnL; review, never overwrite')
    return result


def digest(result): return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


def apply_recovery(path, result, expected):
    if digest(result) != expected: raise ValueError('Preview changed; inspect a new preview')
    # Separate imported history ledger preserves runtime strategy state and kill latch.
    saved = json.loads(path.read_text()) if path.exists() else {}
    key = ':'.join(o['id'] for o in result['orders'])
    if key in saved:
        if saved[key] != result: raise ValueError('Conflicting existing recovery')
        return False
    saved[key] = result
    WriteAheadLog(path.parent / 'journal.wal').snapshot(path, saved)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history-config', type=Path, help='Local JSON with order IDs and expected PnL; never commit it')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-sha256', help='Hash printed by the prior preview')
    args = parser.parse_args()
    async def run():
        cfg = demo_config(); ec = cfg['exchange']
        ex = ReadOnlyDemo(cfg, TokenBucket(ec['rate_per_sec'], ec['rate_burst']))
        try:
            history = json.loads(args.history_config.read_text()) if args.history_config else None
            result = await recover(ex, history)
            print(json.dumps(result, indent=2)); print('preview_sha256=' + digest(result))
            if args.apply:
                changed = apply_recovery(Path(ec['state_dir']) / 'testnet' / 'recovered_history.json', result, args.expected_sha256)
                print('Local history imported' if changed else 'Already imported; no duplicate')
        finally: await ex.close()
    try: asyncio.run(run())
    except Exception as error:
        print(json.dumps({'stage': 'historical_recovery', 'error_type': type(error).__name__,
            'http_status': getattr(error, 'status', 0), 'exchange_error_code': getattr(error, 'code', 0)}))
        raise SystemExit(1) from None


if __name__ == '__main__': main()
