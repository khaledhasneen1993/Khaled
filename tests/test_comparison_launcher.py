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
