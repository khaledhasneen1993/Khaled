"""Native exit reconciliation and distinct liquidation/normal-exit decisions."""
import asyncio
import copy
from decimal import Decimal

from risk.trading_policy import stop_reason
from .orders import ExchangeError, ExecutionUnknown, FillPriceUnavailable, TERMINAL_ORDER_STATUSES as TERMINAL


class ProtectionGuardError(ExecutionUnknown):
    def __init__(self, reason, details=None):
        super().__init__()
        self.reason = reason
        self.details = {'reason': reason, **(details or {})}


def price_exit_reason(pos, price):
    price = Decimal(str(price))
    sign = 1 if pos['side'] == 'buy' else -1
    from risk.trading_policy import stop_reason
    if sign * (price - Decimal(str(pos['sl']))) <= 0: return stop_reason(pos)
    if pos.get('tp') is not None and sign * (price - Decimal(str(pos['tp']))) >= 0: return 'TP'
    return None


class NativeExitState:
    async def retire_protection(self, symbol, pos):
        """After proven flatness, settle parents/children before a new entry."""
        self.execution_stage = 'native_protection_retirement'
        if any(float(r['positionAmt']) != 0 for r in await self.get_position(symbol)):
            raise ProtectionGuardError('position_not_flat_for_retirement', {'symbol': symbol})
        for protection in list(pos.get('protection', {}).values()):
            parent = await self._request('GET', '/fapi/v1/algoOrder',
                {'clientAlgoId': protection['clientOrderId']}, retry=True)
            if parent.get('algoStatus') == 'NEW':
                try: await self.cancel_algo(protection)
                except ExchangeError: pass  # Confirm even if cancellation's response was lost.
                parent = await self._request('GET', '/fapi/v1/algoOrder',
                    {'clientAlgoId': protection['clientOrderId']}, retry=True)
                if parent.get('algoStatus') == 'NEW':
                    self.execution_fault = 'native_cancel_unresolved'
                    raise ProtectionGuardError('native_cancel_unresolved', {'symbol': symbol})
        # FINISHED is an Algo status, not proof its child order is settled.
        await self.inspect_native_exits(symbol, pos, strict=False)

    async def _settle_native_order(self, symbol, order, options):
        self.execution_stage = 'native_exit_settlement'
        record = self._remember_order(symbol, order, options, order['side'].lower())
        record['native_exit'] = True
        self._flush_executions()  # Child ID is durable before any wait or cancel.
        try:
            if order.get('status') not in TERMINAL:
                try:
                    order = await self.settle_fill(symbol, order)
                except ExecutionUnknown:
                    order = self.executions[self.execution_key(symbol, order['orderId'])]['order']
                    if order.get('status') not in TERMINAL:
                        # Stop an outstanding native remainder before another
                        # reducing POST; a lost DELETE response still needs GET.
                        try: await self.cancel_order(symbol, order_id=order['orderId'])
                        except ExchangeError: pass
                        order = await self._request('GET', '/fapi/v1/order', {
                            'symbol': self.raw(symbol), 'orderId': order['orderId']}, retry=True)
                        record = self._remember_order(symbol, order, options, order['side'].lower())
                        if order.get('status') not in TERMINAL:
                            raise ProtectionGuardError('native_exit_unresolved', {'symbol': symbol, 'order_id': order['orderId']})
            record = self._remember_order(symbol, order, options, order['side'].lower())
            if float(order.get('executedQty', 0)) > 0:
                await self._accept_fill(symbol, order, options, order['side'].lower(), recovered=True)
        except FillPriceUnavailable:
            self.remember_unpriced_fill(symbol, order, options, order['side'].lower())
            self.execution_fault = 'fill_price_unavailable'
        return record['order']

    async def inspect_native_exits(self, symbol, pos, strict=False):
        events = []
        for name, protection in list(pos.get('protection', {}).items()):
            self.execution_stage = 'native_protection_query'
            parent = None
            for attempt in range(self.cfg['exchange']['order_poll_attempts'] + 1):
                try:
                    parent = await self._request('GET', '/fapi/v1/algoOrder',
                        {'clientAlgoId': protection['clientOrderId']}, retry=True)
                except ExchangeError as error:
                    if error.code != -2013: raise
                if not parent:
                    if strict: break
                    raise ProtectionGuardError('native_protection_missing', {'symbol': symbol})
                if protection['clientOrderId'] in self.intents:
                    self.resolve_intent(protection['clientOrderId'], parent)
                child = str(parent.get('actualOrderId') or '')
                if child.isdigit() and int(child) > 0: break
                if parent.get('algoStatus') != 'TRIGGERING': break
                if attempt == self.cfg['exchange']['order_poll_attempts']:
                    raise ProtectionGuardError('native_exit_unresolved', {'symbol': symbol})
                await asyncio.sleep(self.cfg['exchange']['order_poll_sec'])
            if not parent:
                events.append({'reason': 'SL' if name == 'sl' else 'TP', 'filled': 0, 'missing': True})
                continue
            expected_side = 'SELL' if pos['side'] == 'buy' else 'BUY'
            if (parent.get('symbol') != self.raw(symbol) or parent.get('side') != expected_side
                    or parent.get('reduceOnly') not in (True, 'true')
                    or parent.get('clientAlgoId') != protection['clientOrderId']
                    or str(parent.get('algoId')) != str(protection['orderId'])):
                raise ProtectionGuardError('native_protection_identity_mismatch', {'symbol': symbol})
            if parent.get('workingType', self.cfg['exchange']['working_type']) != self.cfg['exchange']['working_type']:
                raise ProtectionGuardError('protection_trigger_source_mismatch', {'symbol': symbol})
            if not child.isdigit() or int(child) <= 0:
                if parent.get('algoStatus') == 'NEW': continue
                if not strict and parent.get('algoStatus') in ('CANCELED', 'EXPIRED', 'REJECTED'): continue
                if strict:
                    events.append({'reason': 'SL' if name == 'sl' else 'TP', 'filled': 0, 'missing': True})
                    continue
                raise ProtectionGuardError('native_protection_missing', {'symbol': symbol})
            order = await self._request('GET', '/fapi/v1/order', {
                'symbol': self.raw(symbol), 'orderId': child}, retry=True)
            if (str(order.get('orderId')) != child or order.get('side') != expected_side
                    or order.get('reduceOnly') not in (True, 'true')):
                raise ProtectionGuardError('native_exit_identity_mismatch', {'symbol': symbol})
            options = dict(reduceOnly=True, signal_id=pos['signal_id'], exit_position=copy.deepcopy(pos),
                expected_price=float(protection['trigger']), exit_reason=stop_reason(pos) if name == 'sl' else 'TP',
                native_parent_id=parent['algoId'], native_parent_client_id=protection['clientOrderId'])
            order = await self._settle_native_order(symbol, order, options)
            events.append({'reason': options['exit_reason'], 'trigger': options['expected_price'],
                           'order_id': order['orderId'], 'filled': float(order.get('executedQty', 0))})
        return events

    async def recover_native_exits(self):
        settled = True
        for record in list(self.executions.values()):
            if not record.get('native_exit'): continue
            order = record['order']
            if order.get('status') in TERMINAL and not record.get('fill_price_pending') and (
                    record.get('trade') or float(order.get('executedQty', 0)) == 0):
                continue
            order = await self._request('GET', '/fapi/v1/order', {
                'symbol': self.raw(record['symbol']), 'orderId': order['orderId']}, retry=True)
            if str(order.get('orderId')) != str(record['order']['orderId']):
                raise ProtectionGuardError('native_exit_identity_mismatch', {'symbol': record['symbol']})
            try:
                await self._settle_native_order(record['symbol'], order, record['options'])
            except ProtectionGuardError:
                settled = False
            current = self.executions[self.execution_key(record['symbol'], order['orderId'])]
            if current['order'].get('status') not in TERMINAL or current.get('fill_price_pending'): settled = False
        return settled
