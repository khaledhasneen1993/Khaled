import json
import time
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from test_live_transport import live, run, runtime_with_transport
from exchange.orders import ExchangeError


class MonitorStopped(BaseException):
    pass


@pytest.mark.parametrize('stage', ['account_refresh', 'reconciliation', 'evidence_sample', 'state_save'])
def test_monitor_stage_is_recorded_without_exception_text(live, tmp_path, stage):
    sensitive = 'secret https://example.test/private?signature=do-not-log-this'
    async def check():
        bot, _ = await runtime_with_transport(live, tmp_path)
        await live.refresh_account()
        safety = bot.safety
        now = time.time()
        safety.last_account = safety.last_recon = safety.last_spread = safety.last_funding = safety.last_evidence = now
        safety.last_report_day = time.strftime('%Y-%m-%d', time.gmtime())
        safety.trip = AsyncMock(side_effect=MonitorStopped())
        if stage == 'account_refresh':
            safety.last_account = 0
            live.refresh_account = AsyncMock(side_effect=ExchangeError(503, -1007))
        elif stage == 'reconciliation':
            safety.last_recon = 0
            live.refresh_account = AsyncMock()
            safety.reconcile_once = AsyncMock(side_effect=KeyError(sensitive))
        elif stage == 'evidence_sample':
            safety.last_evidence = 0
            safety.evidence = Mock()
            safety.evidence.sample.side_effect = ValueError(sensitive)
        else:
            bot.save = Mock(side_effect=OSError(sensitive))
        with pytest.raises(MonitorStopped): await safety.monitor()
        safety.trip.assert_awaited_once_with('monitor_failure')
        content = (safety.journal.root / 'events.log').read_text()
        records = [json.loads(line) for line in content.splitlines()]
        record = next(r for r in records if r['event'] == 'monitor_exception')
        assert record['stage'] == stage and record['error_type']
        if stage == 'account_refresh':
            assert record['http_status'] == 503 and record['exchange_error_code'] == -1007
        assert sensitive not in content and 'signature=' not in content
        assert record['source'] == 'infra/supervisor.py'
        assert record['function'] == 'monitor' and type(record['line']) is int
        await live.close()
    run(check())


def test_transport_type_retained_without_url_or_error_body(live):
    async def handler(request):
        raise httpx.ReadTimeout('secret signed URL ' + str(request.url), request=request)
    async def check():
        from infra.errors import diagnostic_fields
        await live.client.aclose()
        live.client = httpx.AsyncClient(base_url='https://example.test', transport=httpx.MockTransport(handler))
        with pytest.raises(ExchangeError) as caught:
            await live._request('GET', '/fapi/v1/accountConfig')
        fields = diagnostic_fields(caught.value)
        assert fields['transport_error_type'] == 'ReadTimeout'
        assert 'signature=' not in json.dumps(fields) and 'example.test' not in json.dumps(fields)
        assert 'secret' not in str(caught.value)
        await live.close()
    run(check())


def test_flat_monitor_completes_real_reconciliation_evidence_and_save(live, tmp_path):
    from exchange.rollout import TestnetEvidence
    from unittest.mock import patch
    async def check():
        bot, _ = await runtime_with_transport(live, tmp_path)
        await live.refresh_account()
        bot.cfg['rollout']['evidence_file'] = str(tmp_path / 'evidence.json')
        bot.safety.evidence = TestnetEvidence(bot.cfg)
        bot.safety.last_report_day = time.strftime('%Y-%m-%d', time.gmtime())
        with patch('infra.supervisor.asyncio.sleep', side_effect=MonitorStopped()):
            with pytest.raises(MonitorStopped): await bot.safety.monitor()
        assert not bot.safety.kill.triggered
        assert bot.safety.last_account and bot.safety.last_recon and bot.safety.last_evidence
        assert json.loads((tmp_path / 'evidence.json').read_text())['samples'][-1]['equity'] == 100
        assert json.loads(bot.state_path.read_text())['operations']['kill']['triggered'] is None
        await live.close()
    run(check())


def test_read_probe_has_no_trade_or_state_mutations(live, tmp_path):
    from tools.monitor_read_probe import probe
    async def check():
        bot, server = await runtime_with_transport(live, tmp_path)
        before = {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
        records = []
        assert await probe(live, 1, 0, records.append) == 0
        assert records[0]['ok'] and records[0]['positions'] == 0
        assert not server.orders and not server.algos
        assert before == {p: p.read_bytes() for p in tmp_path.rglob('*.json')}
        assert not live.intents and not live.executions
        await live.close()
    run(check())


def test_read_probe_reports_transient_error_without_resending_orders(live, tmp_path):
    from tools.monitor_read_probe import probe
    from unittest.mock import patch
    async def check():
        bot, server = await runtime_with_transport(live, tmp_path)
        refresh = live.refresh_account
        first = True
        async def flaky():
            nonlocal first
            if first:
                first = False
                error = ExchangeError()
                error.transport_error_type = 'ReadTimeout'
                raise error
            return await refresh()
        live.refresh_account = flaky
        records = []
        with patch('tools.monitor_read_probe.asyncio.sleep', new_callable=AsyncMock):
            assert await probe(live, 2, 0, records.append) == 1
        assert records[0]['stage'] == 'account_refresh'
        assert records[0]['transport_error_type'] == 'ReadTimeout'
        assert records[1]['ok'] and not server.orders
        assert 'signature=' not in json.dumps(records)
        await live.close()
    run(check())
