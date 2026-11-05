"""Baseline settings and scoped runner selection; no on-disk mode swapping."""
import json
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

_mode = ContextVar('execution_mode', default=None)


def load_config():
    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / 'baseline.json').read_text())
    if _mode.get() is not None: cfg['exchange']['mode'] = _mode.get()
    # A single install uses the same live state and lock from every cwd.
    for section, keys in (('exchange', ('state_dir',)), ('rollout', (
            'evidence_file', 'approval_file', 'operator_policy_file', 'mainnet_evidence_file',
            'scale_state_file', 'scale_approval_file'))):
        for key in keys:
            value = Path(cfg[section][key])
            if not value.is_absolute(): cfg[section][key] = str(root / value)
    return cfg


@contextmanager
def execution_mode(mode):
    if mode not in ('paper', 'testnet'): raise ValueError('Supported runner refuses Mainnet')
    token = _mode.set(mode)
    try: yield
    finally: _mode.reset(token)
