"""Read local evidence without connecting to Binance or authorizing execution."""
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exchange.rollout import TestnetEvidence, strategy_fingerprint, code_fingerprint
from risk.config import load_config


def main():
    cfg = load_config()
    result = {'selected_mode': cfg['exchange']['mode'], 'mainnet_authorized_by_this_tool': False,
              'config_hash': strategy_fingerprint(cfg), 'code_hash': code_fingerprint(), 'evidence': {}}
    for mode, key in (('testnet', 'evidence_file'), ('mainnet', 'mainnet_evidence_file')):
        path = Path(cfg['rollout'][key])
        if not path.is_file():
            result['evidence'][mode] = {'status': 'not_run_or_unavailable'}
            continue
        evidence = TestnetEvidence(cfg, mode)
        result['evidence'][mode] = {'monitored_days': evidence.data['monitored_seconds'] / 86400,
            'continuous_days': evidence.data['continuous_seconds'] / 86400,
            'technical_kills': evidence.data['technical_kills'],
            'reconcile_mismatches': evidence.data['reconcile_mismatches'], **evidence.metrics(),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
