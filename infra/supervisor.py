"""Safety orchestration around existing signal and exit calculations."""
import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

from infra.alerts import TelegramAlerter
from infra.journal import OperationalJournal, atomic_json
from risk.kill_switch import KillSwitch
from risk.exposure_limits import can_open
from risk.funding import check_funding_before_settlement
from risk.slippage import check_slippage_health


class SafetySupervisor:
    def __init__(self, bot):
        self.bot, self.cfg = bot, bot.cfg
        self.live = bot.execution_mode != 'paper'
        root = Path(self.cfg['exchange']['state_dir']) / bot.execution_mode if self.live else bot.state_path.parent
        self.journal = OperationalJournal(root / 'journal')
        self.alerter = TelegramAlerter(self.cfg['alerts'])
        self.kill = KillSwitch(self.cfg['kill_switch'])
        self.spreads = {}
        self.latest = {}
        self.active = self.live
        self.ready = not self.live  # Existing offline paper callers remain usable.
        self.last_recon = self.last_funding = self.last_evidence = self.last_spread = self.last_account = 0.
        self.last_report_day = None
        self.warned = set()
        self.open_orders = {}
        self.evidence = None
        if bot.execution_mode in ('testnet', 'mainnet'):
            from exchange.rollout import TestnetEvidence
            self.evidence = TestnetEvidence(self.cfg, bot.execution_mode)

    def dump(self):
        return {'kill': self.kill.dump(), 'last_report_day': self.last_report_day,
                'announced_kill': 'kill_announced' in self.warned}

    def restore(self, value):
        self.kill.restore(value['kill'])
        self.last_report_day = value.get('last_report_day')
        if value.get('announced_kill'): self.warned.add('kill_announced')
        if self.kill.triggered:
            self.ready = False

    async def event(self, name, level='info', **fields):
        if self.journal:
            self.journal.log(name, **fields)
        await self.alerter.send(name + ' ' + json.dumps(fields, ensure_ascii=False), level)

    def heartbeat(self):
        self.kill.on_ws_heartbeat()

    def observe_book(self, symbol, book):
        if not book['bids'] or not book['asks']: return
        bid, ask = float(book['bids'][0][0]), float(book['asks'][0][0])
        if bid <= 0 or ask < bid: return
        spread = (ask - bid) / ((ask + bid) / 2)
        history = self.spreads.setdefault(symbol, deque(maxlen=self.cfg['kill_switch']['spread_window']))
        # Never include the candidate spike in its own reference mean.
        if len(history) >= self.cfg['kill_switch']['spread_warmup']:
            self.latest[symbol] = SimpleNamespace(avg_spread=sum(history) / len(history), spread=lambda: spread)
        history.append(spread)

    async def allow_entry(self, symbol, side, price):
        if not self.ready or self.kill.triggered:
            return False
        market = self.latest.get(symbol)
        if reason := self.kill.check(self.bot.exchange, market):
            await self.trip(reason)
            return False
        notional = self.bot.exchange.equity * self.cfg['margin_fraction_per_position'] * self.cfg['leverage']
        if self.bot.execution_mode == 'mainnet':
            notional *= self.bot.exchange.allocation_fraction
        allowed, reason = can_open(self.bot, symbol, side, notional, self.cfg['exposure'])
        if not allowed:
            await self.event('exposure_block', 'warning', symbol=symbol, reason=reason)
        return allowed

    async def closed(self, trade):
        detail = trade['exit_detail']
        self.kill.on_trade_closed(detail['net_pnl_usdt'])
        await self.event('trade_closed', symbol=trade['symbol'], reason=detail['exit_reason'], pnl=detail['net_pnl_usdt'])
        if self.kill.check(self.bot.exchange, None):
            # Caller holds the trade lock: monitor will flatten on its next tick.
            self.ready = False
        if warning := check_slippage_health(self, self.cfg['slippage']):
            await self.event('slippage_high', 'warning', reason=warning)

    def last_n(self, n):
        return [t for t in self.bot.exchange.trades if not t['reduceOnly']][-n:]

    async def trip(self, reason):
        first = self.kill.triggered is None
        self.kill.triggered = self.kill.triggered or reason
        self.ready = False
        self.bot.save()  # Latch is durable before any market request.
        if first or 'kill_announced' not in self.warned:
            self.warned.add('kill_announced')
            if self.evidence and reason not in ('daily_drawdown', 'consecutive_losses', 'spread_spike', 'process_shutdown'):
                self.evidence.data['technical_kills'] += 1
                atomic_json(self.evidence.path, self.evidence.data)
            self.bot.save()
            await self.event('kill_triggered', 'critical', reason=self.kill.triggered)
        # Repeat while latched. Failed flattening never removes remaining stops.
        remaining_orders = []
        for _ in range(self.cfg['kill_switch']['flatten_attempts']):
            try:
                if self.live:
                    await self.bot.exchange.refresh_account()
                for symbol in list(self.bot.exchange.positions):
                    try:
                        await self.bot.close_position(symbol, 'kill_switch')
                    except Exception:
                        await self.event('kill_close_failed', 'critical', symbol=symbol)
                if self.live:
                    for order in await self.bot.exchange.get_open_orders():
                        # Retain protective reduce-only orders if flatten failed.
                        symbol = self.bot.exchange.symbol(order['symbol'])
                        protective = bool(order.get('reduceOnly')) or order.get('closePosition') is True
                        if symbol in self.bot.exchange.positions and protective:
                            continue
                        try:
                            if order.get('is_algo'): await self.bot.exchange.cancel_algo(order)
                            else: await self.bot.exchange.cancel_order(order['symbol'], order_id=order['orderId'])
                        except Exception:
                            await self.event('kill_cancel_failed', 'critical', symbol=symbol)
                    await self.bot.exchange.refresh_account()
                    remaining_orders = await self.bot.exchange.get_open_orders()
                if not self.bot.exchange.positions and not remaining_orders:
                    for symbol in list(self.bot.positions):
                        if not await self.bot.adopt_native_close(symbol):
                            self.bot.remove_position(symbol)
                            await self.event('kill_phantom_removed', 'warning', symbol=symbol)
                    break
            except Exception:
                await self.event('kill_exchange_unreachable', 'critical', reason=reason)
            await asyncio.sleep(self.cfg['kill_switch']['flatten_retry_sec'])
        if self.bot.exchange.positions or remaining_orders:
            await self.event('kill_flatten_incomplete', 'critical', reason=self.kill.triggered)
        self.bot.save()

    async def reconcile_once(self):
        from exchange.reconciler import reconcile
        report = await reconcile(self.bot.exchange, self.bot, self.journal)
        if any(report.values()):
            if self.evidence:
                self.evidence.data['reconcile_mismatches'] += 1
                atomic_json(self.evidence.path, self.evidence.data)
            await self.event('reconcile_mismatch', 'critical', report=report)
            await self.trip('reconcile_mismatch')
            return False
        return True

    async def startup(self):
        self.active = True
        if not self.live:
            self.ready = True
            return
        from exchange.rollout import require_operator_policy
        require_operator_policy(self.cfg, self.bot.execution_mode)
        if not self.alerter.enabled:
            raise ValueError('Live modes require TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
        # A successful preflight notification must be drained before entries.
        worker = asyncio.create_task(self.alerter._worker())
        try:
            await self.alerter.send('Khaled startup preflight: ' + self.bot.execution_mode)
            await self.alerter.queue.join()
            if self.alerter.failed:
                raise ValueError('Telegram preflight delivery failed')
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if not await self.reconcile_once():
            raise ValueError('Reconciliation changed state; review and reset the latch before restart')
        # Resolve durable pending intents conservatively before allowing entries.
        for client_id, intent in self.bot.exchange.intents.items():
            if intent['status'] != 'pending': continue
            order = await self.bot.exchange.query_order_by_client_id(intent['symbol'], client_id)
            if order:
                self.bot.exchange.resolve_intent(client_id, order)
                await self.event('order_adopted_on_restart', 'warning', symbol=intent['symbol'], client_id=client_id)
            # Even confirmed absence after restart is not permission to resend.
            await self.trip('pending_order_review')
            raise ValueError('Unresolved order intent requires operator review')
        for symbol, pos in self.bot.positions.items():
            await self.bot.exchange.protect(symbol, pos)
        if self.kill.triggered:
            await self.trip(self.kill.triggered)
            raise ValueError('Kill switch remains latched; manual review required')
        self.kill.on_ws_heartbeat()  # bounded connection warmup grace
        self.ready = True
        if self.evidence: self.evidence.start_session()

    async def watchdog(self):
        # Independent of a slow REST operation holding the trading lock. Entry
        # gating latches immediately; the serialized monitor performs flattening.
        while True:
            if self.kill.check(self.bot.exchange, None):
                self.ready = False
                self.bot.save()
            await asyncio.sleep(self.cfg['kill_switch']['monitor_interval_sec'])

    async def monitor(self):
        while True:
            async with self.bot.lock:
                now = time.time()
                try:
                    if self.live and now - self.last_account >= self.cfg['exchange']['account_refresh_sec']:
                        await self.bot.exchange.refresh_account()
                        self.last_account = now
                    if self.alerter.failed and self.live:
                        await self.trip('alerts_unavailable')
                    if getattr(self.bot.exchange, 'execution_fault', None):
                        await self.trip(self.bot.exchange.execution_fault)
                    if self.live and now - self.last_spread >= self.cfg['kill_switch']['spread_poll_sec']:
                        for symbol in list(self.bot.positions):
                            book = await self.bot.exchange.fetch_order_book(symbol, self.cfg['kill_switch']['book_depth'])
                            self.observe_book(symbol, book)
                            self.kill.check(self.bot.exchange, self.latest.get(symbol))
                        self.last_spread = now
                    reason = self.kill.check(self.bot.exchange, None)
                    if reason:
                        await self.trip(reason)
                    elif self.live and now - self.last_recon >= self.cfg['recon']['interval_sec']:
                        await self.bot.exchange.refresh_account()
                        await self.reconcile_once()
                        self.last_recon = now
                    if not self.kill.triggered and self.live and now - self.last_funding >= self.cfg['funding']['poll_sec']:
                        await check_funding_before_settlement(self.bot, self.bot.exchange, self.cfg['funding'], self.journal)
                        self.last_funding = now
                    if self.kill.state.api_errors_1m >= self.cfg['kill_switch']['api_warning_count'] and 'api' not in self.warned:
                        self.warned.add('api')
                        await self.event('api_errors_warning', 'warning', count=self.kill.state.api_errors_1m)
                    if self.kill.state.api_errors_1m < self.cfg['kill_switch']['api_warning_count']:
                        self.warned.discard('api')
                    age = now - self.kill.state.last_ws_heartbeat
                    if age > self.cfg['kill_switch']['ws_warning_sec'] and 'ws' not in self.warned:
                        self.warned.add('ws')
                        await self.event('ws_disconnected', 'warning')
                    if age <= self.cfg['kill_switch']['ws_warning_sec']: self.warned.discard('ws')
                    utc = datetime.now(timezone.utc)
                    day = utc.date().isoformat()
                    if day != self.kill.day and not self.kill.triggered:
                        self.kill.reset_daily()
                    if utc.hour == self.cfg['alerts']['daily_report_hour_utc'] and self.last_report_day != day:
                        await self.daily_report()
                        self.last_report_day = day
                    if self.evidence and not self.kill.triggered and now - self.last_evidence >= self.cfg['rollout']['equity_sample_sec']:
                        self.evidence.sample(self.bot.exchange.equity, self.bot.exchange.trades, self.bot.positions)
                        self.last_evidence = now
                    self.bot.save()
                except Exception:
                    self.kill.on_api_error()
                    await self.trip('monitor_failure')
            await asyncio.sleep(self.cfg['kill_switch']['monitor_interval_sec'])

    async def daily_report(self):
        report_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        trades = [t for t in self.bot.exchange.trades if t.get('exit_detail')
                  and datetime.fromisoformat(t['timestamp']).date() == report_day]
        wins = sum(t['exit_detail']['net_pnl_usdt'] > 0 for t in trades)
        metrics = self.evidence.metrics() if self.evidence else {}
        report = {'equity': self.bot.exchange.equity, 'daily_pnl': sum(t['exit_detail']['net_pnl_usdt'] for t in trades), 'day_utc': report_day.isoformat(),
                  'trade_count': len(trades), 'win_rate': wins / len(trades) if trades else None, **metrics}
        await self.event('daily_report', report=report)
        if self.live:
            atomic_json(Path(self.cfg['exchange']['state_dir']) / self.bot.execution_mode / 'daily_report.json', report)
