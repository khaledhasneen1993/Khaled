# Khaled 4.1.0 validation — 2026-11-06

Source base: main at `c05506f110cc51bf8c784e62d44305af0df82c5b` (4.0.2).

Status: source implementation and offline validation completed; **72-hour Demo soak pending**. No claim of two-week unattended readiness or Mainnet approval. No Demo/Mainnet orders were sent during development. Final was not modified. No phone runtime was accessed. Binance and Telegram credentials are absent in this environment.

## Changes and retained behavior

- Retained entry signals, freshness checks, 10x leverage, position/risk sizing, TP/SL and trailing calculations. The trading/exposure/ATR/exit configuration blocks compare equal to 4.0.2. Strategy edits only report exit-worker liveness and classify transient failures for retry.
- Demo endpoint resolves to `observe` despite the existing transport/state name `testnet`. Independent testnet policy is `soft`; Mainnet always resolves to `hard` and hard thresholds, including flatten policy. Approved runners continue to refuse Mainnet.
- Observe records and alerts without account flattening or kill-based entry pauses. Mandatory reconciliation, valid order facts, storage durability, per-symbol protection recovery and strategy filters remain gates.
- Soft retains management and recovers after cooldown, healthy conditions and reconciliation under the trading lock. Hard policy's legacy regression coverage is now explicitly selected by simulated fixtures instead of assuming Demo must flatten.
- All financial request mutations log to fsynced WAL before sending. Execution ledgers and account snapshots append/fsync WAL before atomic replacement. Latest snapshots are recovered even when JSON is corrupt or stale; only a torn final WAL line is ignored. Internal WAL corruption fails closed. Replaying never resends financial requests. Existing same-client-ID reconciliation adopts real fills.
- Full disk prevents unjournaled financial requests, including emergency orders. Existing native protection stays. A durable storage probe and reconciliation are required before recovered storage authorizes entries. Prior tests expecting orders despite failed fsync were updated to the explicit new WAL requirement.
- SIGTERM/SIGINT stop entries, allow up to two seconds for the locked action to drain, then persist, alert with a bounded wait, close clients and mark the session clean. They do not flatten or cancel protective orders. An unresolved in-flight POST still requires startup recovery; protection cannot be asserted before its confirmation.
- Bounded GET request budgets and a single-probe circuit breaker; POST uncertainty continues through existing durable intent recovery, never automatic blind retry. WebSocket backoff/jitter wraps the existing parser and fresh-event gates.
- Watchdog resource limits and component liveness, localhost health HTTP, structured logging, independent Telegram health watcher, portable Termux supervisor and systemd units. Systemd uses journald and unlimited restart retries; logrotate covers structured operational files.

## Offline checks

Full-suite result is recorded in the final verification below. Test sockets to external services are forbidden; HTTP execution uses simulated exchange transports.

The 12 chaos cases pass:

| Case | Injected fault and assertion |
|---|---|
| 30s WS gap | Advance the reconnect-policy clock; no kill-based stop; reconnection resets the retry policy |
| 10min WS gap | Advance heartbeat age; Demo records observe; Mainnet hard latches |
| 20 API failures | Inject timeouts into real breaker calls; Demo does not halt |
| 100 API failures | Feed the hard-policy error window; hard latches and requests flatten |
| Process SIGKILL | Kill an actual subprocess after WAL fsync and before JSON replacement; recover the exact snapshot; inspect restart unit policy |
| Corrupt state | Corrupt JSON and tear the WAL tail; recover exact state, append safely, compact and replay |
| Full disk | Reject WAL writes before entry; no financial POST; retain responsive degraded health and block entries |
| Memory/FD limit | Inject resource readings above thresholds; assert watchdog exit codes; no real memory exhaustion |
| Network partition | Advance 5min outage and cooldown clocks; closed/open/half-open recovery and WS reconnection |
| Concurrent restart | Hold the real account file lock; a second runtime refuses startup without a duplicate entry |
| Crash after POST | Execute on a simulated exchange, abort before response; new Demo runtime reconciles/adopts/protects once |
| Graceful trade shutdown | Cancel an actual running runtime via the idempotent signal handler path; assert open native stops and persisted state, no market close, completion below5s |

Additional tests verify forced environments, soft cooldown/health recovery, a single concurrent half-open probe, failure of that probe and reopening, interior WAL corruption rejection, hung-component exit, fast read-only health snapshots, and Mainnet hard spread/loss responses. Existing regression tests retain exact fills, fees, protection replacement, loss accounting, quantity limits and secret-redaction coverage.

A separate local real-HTTP health measurement made100 requests: mean2.69ms, maximum52.54ms, below100ms. It used a synthetic in-memory account, not Binance. This does not prove latency under a72h workload.

Other checks: Python compilation, `git diff --check`, shell syntax for the installer, trading-config equality, and the repository secret-pattern audit. The secret scan reported no candidate files; pattern scanning is not proof that no historical secret exists.

## Operational acceptance still pending

| Required evidence | Actual status |
|---|---|
|72 continuous hours on Demo |Not run: no credentials or connected phone/host runtime |
|Real automatic restart count |Not measured; no service installed or trading started here |
|Real observe incidents, trade count and PnL |Not measured; no invented values |
|Systemd restart after process crash |Unit configuration checked; host installation/restart not exercised |
|Two-week memory/FD/alert delivery reliability |Not established by unit tests |
|Every financial execution accounted against Binance |Offline crash/replay scenarios pass; requires real exchange ledger review after soak |
|Telegram notification within30s after process death |Independent10s watcher provided; requires a running watcher, working network and Telegram |

On the user's configured host, run Demo with one supervisor and `tools/watch_health.py` separately, then run `python tools/soak_demo.py --hours 72`. It collects health latency, uptime resets, resource maxima, trade/PnL and observe counts. Only a full elapsed window with no health gaps/restarts or limit breaches can pass its automated criteria. A shortened/interrupted run remains pending. The collector validates that it is observing the Demo process in observe mode. Review native protection, reconciliation and the Binance ledger in addition to this report before adopting any longer-running readiness claim.

## Final verification

**330 tests passed, plus9 subtests**, including12/12 chaos cases. Command: `python -m pytest -q --timeout=60 --disable-warnings`; elapsed86.96s. The final health/environment checks also passed after adding the Demo-only soak identity check. No trading service or72-hour soak was started.

Historical identifiers and realized PnL were removed from the published baseline/README. Optional historical recovery now requires a local history-config file. Regression examples use synthetic data; trading logic is unchanged.
