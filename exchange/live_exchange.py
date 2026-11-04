"""Binance USD-M execution. Existing PaperExchange call signatures are retained.

All signed requests use environment credentials; all HTTP errors are sanitized.
Native conditional protection uses the Futures Algo API. Requires one-way mode
and a dedicated single-asset USDT-M account. Mainnet is gated before construction.
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from urllib.parse import urlencode

import httpx

from paper_account import PaperExchange, make_position
from infra.journal import OperationalJournal, atomic_json
from infra.rate_limit import TokenBucket, with_backoff
from risk.slippage import entry_slippage_bps, exit_slippage_bps
from .orders import ExchangeError, ExecutionUnknown, safe_create_order


class LiveExchange(PaperExchange):
    def __init__(self, cfg: dict, rate_limiter):
        self.cfg = cfg
        self.mode = cfg['exchange']['mode']
        if self.mode not in ('testnet', 'mainnet'):
            raise ValueError('LiveExchange requires testnet or mainnet mode')
        if self.mode == 'mainnet':
            from .rollout import require_mainnet_readiness
            require_mainnet_readiness(cfg)
        key, secret = os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET')
        if not key or not secret:
            raise ValueError('Set BINANCE_API_KEY and BINANCE_API_SECRET locally')
        if any(not value.isprintable() or any(char.isspace() for char in value) for value in (key, secret)):
            raise ValueError('API credentials contain invalid whitespace or control characters')
        self.allocation_fraction = 1.
        if self.mode == 'mainnet':
            from .rollout import mainnet_allocation_fraction
            self.allocation_fraction = mainnet_allocation_fraction(cfg)
        self._secret = secret
        ec = cfg['exchange']
        self.client = httpx.AsyncClient(base_url=ec['base_url_' + self.mode],
            headers={'X-MBX-APIKEY': key}, timeout=ec['http_timeout_sec'], follow_redirects=False)
        self.rl = rate_limiter
        self.weights = TokenBucket(ec['weight_per_minute'] / 60, ec['weight_per_minute'])
        self.orders_short = TokenBucket(ec['orders_per_10sec'] / 10, ec['orders_per_10sec'])
        self.orders_long = TokenBucket(ec['orders_per_minute'] / 60, ec['orders_per_minute'])
        self.market_data = self
        self.fee_rate = cfg['fee_rate']
        self.leverage = cfg['leverage']
        self.balance = self._equity = self._free = self._used = 0.
        self.positions, self.prices, self.markets = {}, {}, {}
        self.trades = []
        self.clock_offset_ms = 0
        self._last_sync = 0.
        self._blocked_until = 0.
        self._leverage_set = set()
        self._leverage_lock = asyncio.Lock()
        self.journal = OperationalJournal(Path(ec['state_dir']) / self.mode / 'journal')
        self.intent_path = Path(ec['state_dir']) / self.mode / 'order_intents.json'
        self.intents = json.loads(self.intent_path.read_text()) if self.intent_path.exists() else {}
        self.on_api_error = None
        self.pending_protection = {}
        self.execution_fault = None

    @property
    def equity(self): return self._equity
    @property
    def free(self): return self._free
    @property
    def used(self): return self._used

    def persist_intent(self, client_id, params):
        self.intents[client_id] = {'symbol': params['symbol'], 'type': params['type'],
            'side': params['side'], 'amount': params['amount'],
            'params': {k: v for k, v in params['params'].items() if k in
                       ('reduceOnly', 'expected_price', 'exit_atr', 'atr_source', 'stopPrice', 'round_up', 'newClientOrderId', 'signal_id')},
            'status': 'pending'}
        atomic_json(self.intent_path, self.intents)

    def resolve_intent(self, client_id, result):
        self.intents[client_id]['status'] = 'observed'
        self.intents[client_id]['order_id'] = result.get('orderId', result.get('algoId'))
        atomic_json(self.intent_path, self.intents)

    def reject_intent(self, client_id):
        self.intents[client_id]['status'] = 'rejected'
        atomic_json(self.intent_path, self.intents)

    def _sign(self, params):
        value = {**params, 'timestamp': int(time.time() * 1000 + self.clock_offset_ms),
                 'recvWindow': self.cfg['exchange']['recv_window_ms']}
        query = urlencode(value)
        return {**value, 'signature': hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()}

    async def _request(self, method, path, params=None, signed=True, retry=False):
        async def once():
            delay = self._blocked_until - time.monotonic()
            if delay > 0: await asyncio.sleep(delay)
            await self.rl.acquire()
            weight = self.cfg['exchange']['http_weights'].get(path, self.cfg['exchange']['http_weights']['default'])
            await self.weights.acquire(weight)
            if method == 'POST' and path in ('/fapi/v1/order', '/fapi/v1/algoOrder'):
                await self.orders_short.acquire()
                await self.orders_long.acquire()
            values = self._sign(params or {}) if signed else (params or {})
            try:
                response = await self.client.request(method, path, params=values)
            except httpx.TransportError:
                if self.on_api_error: self.on_api_error()
                raise ExchangeError() from None
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code >= 400 or isinstance(data, dict) and int(data.get('code', 0)) < 0:
                code = data.get('code', 0) if isinstance(data, dict) else 0
                if self.on_api_error and code not in self.cfg['exchange']['non_incident_error_codes']:
                    self.on_api_error()
                try: after = float(response.headers.get('Retry-After', 0))
                except ValueError: after = self.cfg['exchange']['max_backoff_sec']
                if response.status_code in (418, 429):
                    after = max(after, self.cfg['exchange']['max_backoff_sec'] if response.status_code == 418 else self.cfg['exchange']['backoff_base'])
                    self._blocked_until = max(self._blocked_until, time.monotonic() + after)
                raise ExchangeError(response.status_code, data.get('code', 0) if isinstance(data, dict) else 0, after)
            return data
        return await with_backoff(once) if retry else await once()

    async def sync_time(self):
        start = time.time() * 1000
        server = await self._request('GET', '/fapi/v1/time', signed=False, retry=True)
        end = time.time() * 1000
        self.clock_offset_ms = server['serverTime'] - (start + end) / 2
        self._last_sync = time.monotonic()

    async def load_markets(self):
        await self.sync_time()
        data = await self._request('GET', '/fapi/v1/exchangeInfo', signed=False, retry=True)
        for raw in data['symbols']:
            if raw.get('contractType') != 'PERPETUAL' or raw.get('quoteAsset') != 'USDT':
                continue
            symbol = raw['baseAsset'] + '/USDT:USDT'
            filters = {f['filterType']: f for f in raw['filters']}
            lot = filters.get('MARKET_LOT_SIZE', filters['LOT_SIZE'])
            if Decimal(lot['stepSize']) == 0:
                lot = filters['LOT_SIZE']
            self.markets[symbol] = {'id': raw['symbol'], 'symbol': symbol, 'linear': True, 'swap': True,
                'settle': raw['marginAsset'], 'active': raw['status'] == 'TRADING', 'filters': filters,
                'step': lot['stepSize'], 'tick': filters['PRICE_FILTER']['tickSize'],
                'limits': {'amount': {'min': float(lot['minQty']), 'max': float(lot['maxQty'])},
                           'cost': {'min': float(filters.get('MIN_NOTIONAL', {}).get('notional', 0))}}}
        # Do not silently change global account position/multi-asset modes.
        hedge = await self._request('GET', '/fapi/v1/positionSide/dual', retry=True)
        multi = await self._request('GET', '/fapi/v1/multiAssetsMargin', retry=True)
        if hedge['dualSidePosition'] or multi['multiAssetsMargin']:
            raise ValueError('Dedicated one-way, single-asset Futures account required')
        await self.refresh_account()
        return self.markets

    def market(self, symbol):
        if symbol in self.markets:
            return self.markets[symbol]
        return next(m for m in self.markets.values() if m['id'] == symbol)

    async def fetch_market(self, symbol): return self.market(symbol)
    def symbol(self, raw): return self.market(raw)['symbol']
    def raw(self, symbol): return self.market(symbol)['id']

    def amount_to_precision(self, symbol, amount):
        step = Decimal(self.market(symbol)['step'])
        return format((Decimal(str(amount)) / step).to_integral_value(rounding=ROUND_DOWN) * step, 'f')

    def _price(self, symbol, price, round_up=False):
        tick = Decimal(self.market(symbol)['tick'])
        return format((Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_UP if round_up else ROUND_DOWN) * tick, 'f')

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        rows = await self._request('GET', '/fapi/v1/klines', {'symbol': self.raw(symbol), 'interval': timeframe, 'limit': limit}, signed=False, retry=True)
        return [[int(r[0]), *map(float, r[1:6])] for r in rows]

    async def fetch_order_book(self, symbol, limit):
        data = await self._request('GET', '/fapi/v1/depth', {'symbol': self.raw(symbol), 'limit': limit}, signed=False, retry=True)
        return {'bids': [[float(p), float(q)] for p, q in data['bids']], 'asks': [[float(p), float(q)] for p, q in data['asks']]}

    async def fetch_ticker(self, symbol):
        result = await self._request('GET', '/fapi/v1/ticker/price', {'symbol': self.raw(symbol)}, signed=False, retry=True)
        price = float(result['price'])
        if not math.isfinite(price) or price <= 0: raise ValueError('Invalid ticker')
        self.prices[symbol] = price
        return {'last': price}

    async def fetch_tickers(self):
        values = await self._request('GET', '/fapi/v1/ticker/24hr', signed=False, retry=True)
        supported = {m['id']: s for s, m in self.markets.items()}
        return {supported[t['symbol']]: {'quoteVolume': float(t['quoteVolume'])} for t in values if t['symbol'] in supported}

    async def refresh_account(self):
        if time.monotonic() - self._last_sync > self.cfg['exchange']['time_sync_interval_sec']:
            await self.sync_time()
        data = await self._request('GET', '/fapi/v3/account', retry=True)
        # Restrict accounting to the USDT collateral asset.
        asset = next(a for a in data['assets'] if a['asset'] == 'USDT')
        self.balance = float(asset['walletBalance'])
        self._equity = float(asset['marginBalance'])
        self._free = float(asset['availableBalance'])
        self._used = float(asset['initialMargin'])
        remote = await self.get_position(None)
        self.positions = {self.symbol(p['symbol']): {'side': 'buy' if float(p['positionAmt']) > 0 else 'sell',
            'entry': float(p['entryPrice']), 'amount': abs(float(p['positionAmt'])),
            'margin': float(p.get('positionInitialMargin', 0)), 'remote': p}
            for p in remote if float(p['positionAmt']) != 0}
        return data

    async def fetch_balance(self):
        await self.refresh_account()
        return {'USDT': {'free': self.free, 'used': self.used, 'total': self.balance, 'equity': self.equity}}

    async def set_leverage(self, leverage, symbol):
        async with self._leverage_lock:
            if leverage != self.cfg['leverage']: raise ValueError('Leverage differs from configuration')
            if symbol in self._leverage_set: return
            try:
                await self._request('POST', '/fapi/v1/marginType', {'symbol': self.raw(symbol), 'marginType': 'ISOLATED'})
            except ExchangeError as error:
                if error.code != -4046: raise
            result = await self._request('POST', '/fapi/v1/leverage', {'symbol': self.raw(symbol), 'leverage': leverage})
            if int(result['leverage']) != leverage: raise ValueError('Leverage was not confirmed')
            self._leverage_set.add(symbol)

    async def get_position(self, symbol):
        return await self._request('GET', '/fapi/v3/positionRisk', {'symbol': self.raw(symbol)} if symbol else {}, retry=True)

    async def get_open_orders(self, symbol=None):
        params = {'symbol': self.raw(symbol)} if symbol else {}
        normal = await self._request('GET', '/fapi/v1/openOrders', params, retry=True)
        algo = await self._request('GET', '/fapi/v1/openAlgoOrders', params, retry=True)
        return normal + [{**o, 'is_algo': True, 'clientOrderId': o['clientAlgoId'], 'orderId': o['algoId']} for o in algo]

    async def cancel_order(self, symbol, order_id=None, client_id=None):
        return await self._request('DELETE', '/fapi/v1/order', {'symbol': self.raw(symbol),
            **({'orderId': order_id} if order_id is not None else {'origClientOrderId': client_id})})

    async def cancel_algo(self, order):
        return await self._request('DELETE', '/fapi/v1/algoOrder', {'algoId': order['orderId']})

    async def query_order_by_client_id(self, symbol, client_id):
        try:
            if self.intents.get(client_id, {}).get('type') in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
                return await self._request('GET', '/fapi/v1/algoOrder', {'clientAlgoId': client_id}, retry=True)
            return await self._request('GET', '/fapi/v1/order', {'symbol': self.raw(symbol), 'origClientOrderId': client_id}, retry=True)
        except ExchangeError as error:
            if error.code == -2013: return None
            raise

    async def submit_order(self, symbol, type, side, amount, params=None):
        options = params or {}
        client_id = options['newClientOrderId']
        quantity = self.amount_to_precision(symbol, amount)
        if Decimal(quantity) <= 0: raise ValueError('Quantity rounds to zero')
        if type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
            values = {'algoType': 'CONDITIONAL', 'symbol': self.raw(symbol), 'side': side.upper(), 'type': type,
                'quantity': quantity, 'reduceOnly': 'true', 'clientAlgoId': client_id,
                'triggerPrice': self._price(symbol, options['stopPrice'], options['round_up']),
                'workingType': self.cfg['exchange']['working_type']}
            return await self._request('POST', '/fapi/v1/algoOrder', values)
        if type != 'market': raise ValueError('Runtime only uses market and protective conditional orders')
        return await self._request('POST', '/fapi/v1/order', {'symbol': self.raw(symbol), 'side': side.upper(),
            'type': 'MARKET', 'quantity': quantity, 'newClientOrderId': client_id, 'newOrderRespType': 'RESULT',
            **({'reduceOnly': 'true'} if options.get('reduceOnly') else {})})

    async def settle_fill(self, symbol, order):
        for _ in range(self.cfg['exchange']['order_poll_attempts']):
            if order.get('status') in ('FILLED', 'EXPIRED', 'CANCELED', 'REJECTED'):
                if float(order.get('executedQty', 0)) > 0: return order
                raise ExchangeError(400)
            await asyncio.sleep(self.cfg['exchange']['order_poll_sec'])
            order = await self._request('GET', '/fapi/v1/order', {'symbol': self.raw(symbol), 'orderId': order['orderId']}, retry=True)
        raise ExecutionUnknown()

    async def create_order(self, symbol, type, side, amount, params=None):
        try:
            return await self._create_order(symbol, type, side, amount, params)
        except Exception:
            self.journal.log('order_execution_failed', symbol=symbol)
            self.execution_fault = 'order_execution_uncertain'
            raise ExchangeError() from None

    async def _create_order(self, symbol, type, side, amount, params=None):
        options = params or {}
        if not options.get('reduceOnly'):
            await self.set_leverage(self.leverage, symbol)
        expected = float(options.get('expected_price') or (await self.fetch_ticker(symbol))['last'])
        equity = self.equity * self.allocation_fraction
        result = await safe_create_order(self, {'symbol': symbol, 'type': type, 'side': side, 'amount': amount, 'params': options}, self.journal)
        result = await self.settle_fill(symbol, result)
        actual, filled = float(result['avgPrice']), float(result['executedQty'])
        if actual <= 0 or filled <= 0: raise ExecutionUnknown()
        fills = await self._request('GET', '/fapi/v1/userTrades', {'symbol': self.raw(symbol), 'orderId': result['orderId'], 'limit': self.cfg['exchange']['trade_page_limit']}, retry=True)
        if not fills or any(f['commissionAsset'] != 'USDT' for f in fills):
            raise ExecutionUnknown()  # exact net accounting is required
        quantity_sum = sum(float(f['qty']) for f in fills)
        if not math.isclose(quantity_sum, filled, rel_tol=self.cfg['exchange']['fill_qty_tolerance_pct'], abs_tol=0):
            raise ExecutionUnknown()
        fee = sum(float(f['commission']) for f in fills)
        trade = {'id': str(result['orderId']), 'client_id': result['clientOrderId'],
            'timestamp': datetime.now(timezone.utc).isoformat(), 'symbol': symbol, 'side': side,
            'amount': filled, 'price': actual, 'reduceOnly': bool(options.get('reduceOnly')),
            'fee_usdt': fee, 'pnl_usdt': sum(float(f['realizedPnl']) for f in fills),
            'balance_usdt': self.balance, 'mode': self.mode}
        if options.get('reduceOnly'):
            trade['signal_id'] = options.get('signal_id')
            original_side = 'sell' if side == 'buy' else 'buy'
            trade.update(expected_exit=expected, actual_exit=actual,
                         exit_slippage_bps=exit_slippage_bps(expected, actual, original_side))
        else:
            pos = make_position(side, actual, filled, self.leverage,
                options.get('exit_atr', actual * self.cfg['atr']['fallback_atr_pct']), self.cfg, equity,
                max(self.fee_rate, fee / (actual * filled)), atr_source=options.get('atr_source', 'fallback'))
            pos['entry_fee'] = fee
            pos['account_equity_at_entry'] = self.equity
            pos['allocation_fraction'] = self.allocation_fraction
            pos.update(expected_entry=expected, actual_entry=actual,
                       entry_slippage_bps=entry_slippage_bps(expected, actual, side))
            trade.update({k: pos[k] for k in ('expected_entry', 'actual_entry', 'entry_slippage_bps', 'signal_id')})
            self.pending_protection[symbol] = pos
            self.trades.append(trade)
            atomic_json(self.intent_path.parent / 'filled_entries.json', self.trades)
            remote = [p for p in await self.get_position(symbol) if float(p['positionAmt']) != 0]
            if not remote:
                raise ExecutionUnknown()
            liquidation = float(remote[0]['liquidationPrice'])
            margin = self.cfg['tp_sl']['liq_safety_margin']
            safe = (pos['sl'] > liquidation * (1 + margin) if side == 'buy'
                    else pos['sl'] < liquidation * (1 - margin))
            if liquidation <= 0 or not safe:
                raise ExecutionUnknown()  # preserve strategy levels; flatten instead of using an unsafe stop
            try:
                await self.protect(symbol, pos)
            except Exception:
                # The fill is real. Keep its metadata and let the runtime kill
                # path reconcile and flatten; never pretend the entry failed.
                raise ExecutionUnknown() from None
        if options.get('reduceOnly'):
            self.trades.append(trade)
        await self.refresh_account()
        if options.get('reduceOnly') and symbol in self.positions:
            raise ExecutionUnknown()
        return {**trade, 'average': actual, 'filled': filled, 'status': 'closed',
                **({'risk_position': pos} if not options.get('reduceOnly') else {})}

    async def protect(self, symbol, pos):
        """Create-confirm-before-cancel: no unsupported atomic STOP amendment."""
        side = 'sell' if pos['side'] == 'buy' else 'buy'
        for name, kind, target in (('sl', 'STOP_MARKET', pos['sl']), ('tp', 'TAKE_PROFIT_MARKET', pos['tp'])):
            old = pos.get('protection', {}).get(name)
            # Stop rounds toward entry; TP rounds away, preserving planned RR.
            round_up = pos['side'] == 'buy'
            trigger = self._price(symbol, target, round_up)
            if old and old['trigger'] == trigger: continue
            result = await safe_create_order(self, {'symbol': symbol, 'type': kind, 'side': side,
                'amount': pos['amount'], 'params': {'reduceOnly': True, 'stopPrice': float(trigger), 'round_up': round_up}}, self.journal)
            client_id = result['clientAlgoId']
            confirmed = await self.query_order_by_client_id(symbol, client_id)
            if not confirmed or confirmed.get('algoStatus') not in ('NEW',):
                raise ExecutionUnknown()
            new = {'orderId': result['algoId'], 'clientOrderId': client_id, 'trigger': trigger, 'is_algo': True}
            pos.setdefault('protection', {})[name] = new
            # Persist new protection before removing any old order.
            self.pending_protection[symbol] = pos
            atomic_json(self.intent_path.parent / 'protection.json', self.pending_protection)
            if old:
                try: await self.cancel_algo(old)
                except ExchangeError as error:
                    if error.code not in (-2011, -2013): raise

    async def get_funding_rate(self, symbol):
        return await self._request('GET', '/fapi/v1/premiumIndex', {'symbol': self.raw(symbol)}, signed=False, retry=True)

    async def close(self):
        await self.client.aclose()
