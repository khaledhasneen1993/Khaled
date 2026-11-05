# Khaled 3.1.0 — execution review, 2026-11-05

Scope: `khaledhasneen1993/Khaled` only. Remote main was verified as
`ced0bccd1eaeb71ba324301e5402d70b41973ac2` (3.0.3). No local Termux source or
Final repository was used. No Binance account credentials were available or used;
no Demo/Mainnet trading, account mutation, or Telegram messages were performed.

## Proven defects and evidence

The new `test_repro_*` tests were copied into a separate detached checkout of
3.0.3 and run there: **2 failed**, as expected. With `FILLED` followed by empty
`userTrades`, the original adapter raised before creating its trade/position or
SL/TP. Its outer `create_order` also replaced `ExecutionUnknown(503, -1007)` with
`ExchangeError(0, 0)`. These are reproduced code defects, consistent with the
reported symptoms, **not proof of the precise cause of the historical HUMA incident**.

## Implementation

- `exchange/execution.py`: cumulative Binance order facts committed before fee
  lookup, separate pending/complete accounting, Decimal totals, paginated fills,
  symbol + order ID ledger keys, upsert rather than duplicate append, recovery of
  pending and observed-but-unrecorded orders. No network exactly-once claim.
- `exchange/orders.py`, `live_exchange.py`, `infra/journal.py`: durable pre-POST
  intents and raw sanitized order responses, stable ID queries after ambiguity,
  no blind second entry; preserve error type/code and allowlisted diagnostic fields.
  An unresolved prior same-symbol intent blocks another POST. A live remainder
  must be canceled and observed terminal before emergency closing its proven residual.
- Real SL/TP Algo orders remain create → query-confirm → fsync → cancel old.
  Recovery installs missing protection before reconciliation permits entries.
  Failure latches entries and invokes the existing reducing emergency path.
- `paper_runtime.py`, `infra/supervisor.py`, `reconciler.py`, `paper_account.py`:
  merge exchange facts with local strategy/dynamic stops, discover reducing exits
  absent from local intents, retain unattributed phantom metadata, delayed exit
  accounting, preserve the kill latch. Ordinary and Algo orders are reconciled.
- `exchange/leverage.py`: search descending integer leverage against the actual
  account's returned notional brackets and rounded quantity. Leverage POST must
  confirm acceptance and cap. Definitive account leverage-limit rejection searches
  down; transport uncertainty does not authorize an order. Each position and
  execution carries its leverage, margin, bracket MMR and maintenance amount.
  `risk/exposure_limits.py` sums each position's own margin, avoiding division of
  all positions by a single global leverage. Paper compatibility remains 10×.
- Explicit `run_demo.py` refuses a Mainnet baseline or changed endpoints. Scoped
  mode selection never rewrites baseline for Paper tests. Legacy mainnet gates
  remain present; Mainnet is not an approved run path.
- RSA PKCS#8 PEM signing and HMAC share the HTTP transport; private key permission
  checks, sanitized failures, and a generated-key HTTP signature test are included.
- `tools/demo_preflight.py` enforces GET-only calls and checks account reads,
  modes, market-data WS, orders/Algo reads, symbol configuration and candidate
  leverage. It explicitly reports that order permission, leverage acceptance,
  and protection acceptance are **not** established by these reads.
- `tools/recover_huma.py` queries only the supplied two IDs and exact executions.
  Preview precedes hash-bound idempotent import into `recovered_history.json`;
  runtime state and kill history are preserved. Unknown historical ATR, TP, SL,
  leverage and margin remain null. Confirmed PnL conflicts fail rather than overwrite.

## Mathematical correction

For side sign `s = +1` long / `-1` short, quantity Q, entry E, leverage L,
maintenance ratio m and bracket deduction C, isolated initial-margin equity gives

`P_liq ≈ [E(1 − s/L) − s*C/Q] / (1 − s*m)`.

This replaces the first-order `E*(1 − s*(1/L − m))` approximation and accounts for
bracket maintenance deductions. It remains a model: exchange fees, funding,
isolated-wallet adjustments, mark price, and tier changes affect actual liquidation.
Binance's returned liquidation price is independently checked after entry and in
management; the existing conservative stop-distance cap is also retained.

ATR intent is unchanged: raw stop is max(1.5 ATR, 0.8% entry); it may be tightened
by liquidation and the 3% equity loss budget including fees. For fee rate f,
`d_max = (0.03*equity/Q − 2*E*f)/(1 − s*f)`.
Actual entry fees can tighten this cap but never loosen a moved stop. If fees
consume the budget or no positive safe stop exists, opening is refused or an
already confirmed fill enters the emergency path. Maximum leverage can produce
very tight stops. No guaranteed loss cap across gaps, slippage or outages is claimed.

## Verification and remaining limits

Tests are offline: sockets are blocked, real credential variables removed per
test, and Paper mode is scoped without modifying baseline.json. Final validation: **121 passed, 9 subtests passed**. `compileall` and `git diff --check` passed. Fault coverage includes lost POST/close replies,
empty/delayed executions, partial→full orders, crash after POST, restart intent
recovery, lost local close write, unknown remote exit, SL/TP rejection, replacement
persistence failure, orphan Algo cancellation, fee/PnL deduplication, RSA HTTP
signatures, Mainnet rejection, leverage tiers/account limits/current equity, and
per-position leverage persistence and exposure. Existing strategy, time-stop,
funding, monotone stops, dashboard and Paper regression tests remain.

Remaining account acceptance work is deliberate: run GET-only preflight with a
**rotated Demo key**, inspect any current positions/orders and incident state,
verify Telegram and operator controls, then the operator may start Demo. No
private account state is inferred from the historical “zero positions” report.
Preflight cannot prove TRADE permissions or an Algo order's acceptance without a
write. No such write was performed. Network interruptions may leave an intent
blocked for manual review. Data beyond Binance retention, non-USDT fees, or fills
not available within configured pagination remain pending rather than fabricated.
The separate HUMA imported history is not injected into the live risk ledger or
strategy metrics. Dedicated account use and only one running installation are
operator requirements; the file lock protects processes sharing one state directory.
