"""Explicit offline reset after operator review; never starts a trading process."""
import argparse
import fcntl
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infra.journal import OperationalJournal, atomic_json
from risk.kill_switch import KillSwitch
from risk.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--reviewed', action='store_true', help='Confirm the incident has been reviewed manually')
    args = parser.parse_args()
    if not args.reviewed: parser.error('Manual incident review is required')
    path = Path(args.state)
    with (path.parent / 'account.lock').open('a') as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: parser.error('Stop the running account process before reset')
        data = json.loads(path.read_text())
        if data['account_positions'] or data['strategy_positions']:
            parser.error('Positions remain in the saved state; reconcile/flatten and review first')
        cfg = load_config()
        kill = KillSwitch(cfg['kill_switch'])
        kill.reset_daily()
        data['operations']['kill'] = kill.dump()
        data['operations']['announced_kill'] = False
        data['operations']['storage_failed'] = False  # Atomic save below must succeed; no reset on write failure.
        atomic_json(path, data)
        atomic_json(path.parent / cfg['exchange']['session_guard_filename'], {'active': False, 'reviewed': True})
        OperationalJournal(path.parent / 'journal').log('kill_manual_reset')
    print('Local latch reset. Startup reconciliation and all other gates still apply.')


if __name__ == '__main__':
    main()
