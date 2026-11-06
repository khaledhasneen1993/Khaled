import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.run_comparison_dashboard import extract_ui, fresh_environment, validate_source, verify_fresh_accounts

ROOT = Path(__file__).resolve().parents[1]


def test_ui_extracts_assets_only_and_uses_current_khaled(tmp_path):
    cfg = json.loads((ROOT / 'baseline.json').read_text())['comparison_dashboard']
    extract_ui(ROOT, cfg['ui_commit'], tmp_path)
    assert (tmp_path / 'dashboard' / 'server.py').is_file()
    assert not (tmp_path / 'paper_account.py').exists()
    text = (tmp_path / 'dashboard' / 'paths.py').read_text()
    assert 'CASCADE_KHALED_DIR' in text
    assert '"khaled": ROOT,' not in text


def test_fresh_sessions_are_separate_and_strip_exchange_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('BINANCE_API_KEY', 'unit-test-key')
    monkeypatch.setenv('BINANCE_API_SECRET', 'unit-test-secret')
    first, env = fresh_environment(tmp_path, tmp_path / 'Final', tmp_path / 'Khaled')
    second, _ = fresh_environment(tmp_path, tmp_path / 'Final', tmp_path / 'Khaled')
    assert first != second
    assert env['CASCADE_FINAL_STATE'] != env['CASCADE_KHALED_STATE']
    assert 'BINANCE_API_KEY' not in env and 'BINANCE_API_SECRET' not in env
    for name in ('FINAL', 'KHALED'):
        p = Path(env[f'CASCADE_{name}_STATE'])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'balance_usdt': 100, 'account_positions': {}, 'strategy_positions': {}, 'trades': []}))
    verify_fresh_accounts(env)
    p.write_text(json.dumps({'balance_usdt': 99, 'account_positions': {}, 'strategy_positions': {}, 'trades': []}))
    with pytest.raises(ValueError, match='not fresh'):
        verify_fresh_accounts(env)


def test_wrong_repository_is_rejected(tmp_path):
    with patch('tools.run_comparison_dashboard.checked_git', return_value=b'https://github.com/example/Final-3.git\n'):
        with pytest.raises(ValueError, match='not the Final'):
            validate_source(tmp_path, 'Final')


def test_comparison_ui_renders_current_policy_and_keeps_other_bot_labels(tmp_path):
    import shutil
    import subprocess
    node=shutil.which('node')
    if node is None: pytest.skip('Node is only needed to execute the comparison UI rendering test')
    cfg=json.loads((ROOT/'baseline.json').read_text())
    extract_ui(ROOT,cfg['comparison_dashboard']['ui_commit'],tmp_path)
    app=tmp_path/'dashboard'/'app.js'
    subprocess.run([node,'--check',str(app)],check=True,capture_output=True,text=True)
    functions='\n'.join(line for line in app.read_text().splitlines()
                        if line.startswith(('function settingsSummary(s)','function settings()')))
    current={'settings':{'MAX_POSITION_PCT':.1,'LEVERAGE':10,'MAX_CONCURRENT_POSITIONS':4,
        'position_basis':'current_equity','CASCADE_THRESHOLD':.012,'VOLUME_SPIKE':3.5,
        'active_exit':{'exit':cfg['exit'],'tp_sl':cfg['tp_sl']}},'fee_rate':.0005}
    other={'settings':{'MAX_POSITION_PCT':.1,'LEVERAGE':50,'MAX_CONCURRENT_POSITIONS':3,
        'STOP_LOSS_PCT':.15,'TAKE_PROFIT_PCT':.018},'fee_rate':.0005}
    payload={'functions':functions,'current':current,'other':other}
    probe="""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const render=new Function('snapshot','num','ids','data','names','esc','table','exports',
 input.functions+';return {summary:settingsSummary(snapshot),body:settings()}');
function show(snapshot){return render(snapshot,(v,n)=>Number(v).toFixed(n),()=>['test'],()=>snapshot,
 {test:'bot'},v=>String(v),(_,rows)=>JSON.stringify(rows),()=> '');}
process.stdout.write(JSON.stringify({current:show(input.current),other:show(input.other)}));
"""
    result=subprocess.run([node,'-e',probe],input=json.dumps(payload),text=True,capture_output=True,check=True)
    display=json.loads(result.stdout)
    assert 'ATR' not in display['current']['summary'] and 'الوقف من ATR' not in display['current']['body']
    assert '8.0%' in display['current']['summary'] and '1.5%' in display['current']['summary']
    assert '0.6%' in display['current']['summary'] and '2.50%' in display['current']['body']
    assert 'لا TP ثابت' in display['current']['body'] and 'سقف هامش' in display['current']['summary']
    assert '15.0%' in display['other']['summary'] and '1.8%' in display['other']['summary']
