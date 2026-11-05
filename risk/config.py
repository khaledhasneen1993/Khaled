"""Baseline settings and scoped runner selection; no on-disk mode swapping."""
import json
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

_mode = ContextVar('execution_mode', default=None)


def load_config():
    cfg = json.loads((Path(__file__).resolve().parents[1] / 'baseline.json').read_text())
    if _mode.get() is not None: cfg['exchange']['mode'] = _mode.get()
    return cfg


@contextmanager
def execution_mode(mode):
    if mode not in ('paper', 'testnet'): raise ValueError('Supported runner refuses Mainnet')
    token = _mode.set(mode)
    try: yield
    finally: _mode.reset(token)
