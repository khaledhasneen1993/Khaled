"""Validate exit math on real monthly Binance candles; NOT a strategy backtest.

Usage: python tools/validate_exit_history.py --data-dir /path/to/zips --output /path/report.json
Candles lack historical top-20 order books and intrabar stream events. Therefore
this tool intentionally emits no trade counts, win rates, Sharpe or drawdown.
"""
import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from risk.config import load_config
from risk.atr_levels import compute_atr
from risk.liq_math import validate_stop_against_liq, max_safe_stop_pct
from paper_account import make_position


def validate(data_dir, months=None):
    cfg = load_config()
    files = sorted(Path(data_dir).glob('*USDT-1m-????-??.zip'))
    if months:
        files = [path for path in files if path.stem.split('-1m-')[1] in months]
    if not files:
        raise ValueError('No Binance monthly 1m archives found')
    result = {'kind': 'historical_candle_exit_invariants_only', 'files': [], 'symbols': {},
              'strategy_backtest_status': 'blocked_missing_historical_orderbooks_and_intrabar_events',
              'trade_metrics': {key: None for key in ('count_before', 'count_after', 'mean_rr_planned',
                  'trade_stop_capped_fraction', 'stuck_over_60m_fraction', 'exit_reason_distribution',
                  'max_drawdown', 'rolling_100_sharpe')}}
    histories = {}
    for path in files:
        symbol = path.name.split('-')[0]
        summary = result['symbols'].setdefault(symbol, {'candles': 0, 'scenarios': 0,
            'liq_violations': 0, 'equity_risk_violations': 0, 'rr_violations': 0,
            'stop_capped': 0, 'risk_capped': 0, 'rr_sum': 0., 'rr_min': float('inf'),
            'gaps': 0, 'start_ms': None, 'end_ms': None})
        result['files'].append({'filename': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'url': f'https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1m/{path.name}'})
        history = histories.setdefault(symbol, deque(maxlen=cfg['atr']['period'] + 1))
        with zipfile.ZipFile(path) as archive:
            with archive.open(archive.namelist()[0]) as raw:
                rows = {int(row['open_time']): row for row in csv.DictReader(io.TextIOWrapper(raw))}
        # Supplement gaps only with authentic daily archives, never synthetic candles.
        for daily in sorted(Path(data_dir).glob(path.stem + '-??.zip')):
            result['files'].append({'filename': daily.name, 'sha256': hashlib.sha256(daily.read_bytes()).hexdigest(),
                'url': f'https://data.binance.vision/data/futures/um/daily/klines/{symbol}/1m/{daily.name}'})
            with zipfile.ZipFile(daily) as archive:
                with archive.open(archive.namelist()[0]) as raw:
                    for row in csv.DictReader(io.TextIOWrapper(raw)):
                        ts = int(row['open_time'])
                        if ts in rows and rows[ts] != row:
                            raise ValueError('Conflicting daily/monthly candle')
                        rows[ts] = row
        for ts, row in sorted(rows.items()):
            ts = int(row['open_time'])
            if summary['end_ms'] is not None and ts != summary['end_ms'] + 60000:
                summary['gaps'] += 1
                history.clear()
            if summary['start_ms'] is None:
                summary['start_ms'] = ts
            summary['end_ms'] = ts
            summary['candles'] += 1
            history.append({key: float(row[key]) for key in ('high', 'low', 'close')})
            if len(history) < history.maxlen:
                continue
            atr = compute_atr(list(history), cfg['atr']['period'])
            entry = history[-1]['close']
            equity = cfg['initial_balance_usdt']
            amount = equity * cfg['margin_fraction_per_position'] * cfg['leverage'] / entry
            for side in ('buy', 'sell'):
                pos = make_position(side, entry, amount, cfg['leverage'], atr, cfg,
                                    equity, cfg['fee_rate'], opened_at=ts / 1000, signal_id='scenario')
                summary['scenarios'] += 1
                summary['liq_violations'] += not validate_stop_against_liq(side, entry, pos.stop, cfg['leverage'])
                loss = abs(entry - pos.stop) * amount + pos['entry_fee'] + pos.stop * amount * cfg['fee_rate']
                summary['equity_risk_violations'] += loss > equity * cfg['tp_sl']['max_equity_risk_pct'] + 1e-9
                summary['rr_violations'] += pos['rr'] < cfg['tp_sl']['min_rr']
                summary['stop_capped'] += pos['stop_capped']
                summary['risk_capped'] += pos['risk_capped']
                summary['rr_sum'] += pos['rr']
                summary['rr_min'] = min(summary['rr_min'], pos['rr'])
    for summary in result['symbols'].values():
        for key in ('start_ms', 'end_ms'):
            summary[key.replace('_ms', '_utc')] = datetime.fromtimestamp(summary[key] / 1000, timezone.utc).isoformat()
        summary['scenario_mean_rr'] = summary.pop('rr_sum') / summary['scenarios']
        summary['scenario_stop_capped_fraction'] = summary['stop_capped'] / summary['scenarios']
        summary['scenario_risk_capped_fraction'] = summary['risk_capped'] / summary['scenarios']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--months', nargs='+', help='Optional YYYY-MM archive selection')
    args = parser.parse_args()
    result = validate(args.data_dir, args.months)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['symbols'], indent=2))
    if any(s[key] for s in result['symbols'].values() for key in ('gaps', 'liq_violations', 'equity_risk_violations', 'rr_violations')):
        raise SystemExit('Historical exit invariant check failed')


if __name__ == '__main__':
    main()
