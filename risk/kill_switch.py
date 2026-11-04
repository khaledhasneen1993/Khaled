import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass
class KillState:
    daily_pnl: float = 0.
    consecutive_losses: int = 0
    last_ws_heartbeat: float = 0.
    api_errors_1m: int = 0
    last_api_error_ts: float = 0.


class KillSwitch:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = KillState(last_ws_heartbeat=time.time())
        self.triggered = None
        self.errors = deque()
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.day_start_equity = None

    def on_trade_closed(self, pnl: float):
        self.state.daily_pnl += pnl
        self.state.consecutive_losses = self.state.consecutive_losses + 1 if pnl < 0 else 0

    def on_ws_heartbeat(self):
        self.state.last_ws_heartbeat = time.time()

    def _prune(self):
        cutoff = time.time() - self.cfg['api_window_sec']
        while self.errors and self.errors[0] < cutoff:
            self.errors.popleft()
        self.state.api_errors_1m = len(self.errors)

    def on_api_error(self):
        self.errors.append(time.time())
        self.state.last_api_error_ts = self.errors[-1]
        self._prune()

    def reset_daily(self):
        self.state.daily_pnl = 0.
        self.state.consecutive_losses = 0
        self.triggered = None
        self.errors.clear()
        self.state.api_errors_1m = 0
        self.state.last_ws_heartbeat = time.time()
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.day_start_equity = None

    def check(self, state, market):
        self._prune()
        if self.triggered:
            return self.triggered
        equity = float(state.equity)
        if self.day_start_equity is None:
            self.day_start_equity = equity
        # Include unrealized losses; compare with a stable daily starting equity.
        day_loss = min(self.state.daily_pnl, equity - self.day_start_equity)
        if day_loss <= -self.cfg['daily_dd_limit_pct'] * self.day_start_equity:
            self.triggered = 'daily_drawdown'
        elif self.state.consecutive_losses >= self.cfg['max_consecutive_losses']:
            self.triggered = 'consecutive_losses'
        elif time.time() - self.state.last_ws_heartbeat > self.cfg['ws_timeout_sec']:
            self.triggered = 'ws_timeout'
        elif self.state.api_errors_1m >= self.cfg['api_error_limit']:
            self.triggered = 'api_errors'
        elif market is not None and market.avg_spread > 0 and market.spread() > self.cfg['spread_spike_mult'] * market.avg_spread:
            self.triggered = 'spread_spike'
        return self.triggered

    def dump(self):
        return {'state': asdict(self.state), 'triggered': self.triggered, 'day': self.day,
                'day_start_equity': self.day_start_equity, 'errors': list(self.errors)}

    def restore(self, data):
        self.state = KillState(**data['state'])
        self.triggered = data['triggered']
        self.day = data['day']
        self.day_start_equity = data.get('day_start_equity')
        self.errors = deque(data.get('errors', []))
