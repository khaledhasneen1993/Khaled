# Khaled adaptive execution validation

Status: review candidate. Final-Edge v3 reference commit:
`eb24a32ac04e667f30d33d39f0d23d9efa8eb465`.

## Reproducible execution check

Install the runtime requirements and pytest. Point `KHALED_REFERENCE_SOURCE`
at a checkout of the reference commit and run:

```bash
python scripts/replay_density.py --source "$KHALED_REFERENCE_SOURCE"
python scripts/replay_density.py --source .
python -m pytest -q tests dashboard/tests
```

The same 100 generated BTC/ETH long and short Cascade events passed through
the two scanners. Both opened 100 positions. Each scenario starts with a new
100 USDT account. Fixtures provide narrow spreads, available depth and fresh
data. In the adaptive replay, confirmation and passive waiting use zero
duration to exercise the unconfirmed IOC path. Production timeouts are 2 and
5 seconds. The passive trade-through path is tested separately. This check
does not measure wallet contention, actual venue queue priority, realized
returns, or historical fill rate.

## Historical acceptance

The Binance historical archive connection timed out in the execution
environment. No matched three-month tape with timestamped book/depth,
aggressor trades and candles for both perpetuals was available. Neither a
candle-only surrogate nor synthetic equity returns were used to score
historical acceptance.

| Criterion | Threshold | Three-month result |
|---|---:|---|
| Executed trades vs. reference | ≥90% | Not measured |
| Trades held over 60 minutes | <5% | Not measured |
| Mean holding time | <40 min | Not measured |
| Realized mean R:R | 1.2 | Not measured |
| Rolling 100-trade Sharpe | 1.0 | Not measured |
| Max drawdown | <12% | Not measured |
| Exit distribution | TP <40%; time 15–30%; BE 15–25%; decay 5–15%; SL <15% | Not measured |

Run both versions on the same event tape and identical fee assumptions.
Record net return after fees, capital use, stop risk and event/decision/fill
timestamps. Use realized net P&L divided by initial risk for trade R, and
mark-to-market equity including open positions for drawdown. Report mean
net winning trade divided by mean absolute net losing trade separately.
Report the unannualized mean divided by sample standard deviation over
100 closed-trade net returns, with sample coverage. Classify by recorded
exit reason. Keep any tuning interval separate from the validation interval.

## Model choices

- Stop safety takes priority over the minimum distance. Sizing reserves two
  fees and execution slippage; risk budgets are targets subject to price gaps.
- Maker fills require observed aggressive volume through the limit. IOC
  requires enough observed depth for its requested fallback quantity within
  the bound. No real exchange orders are submitted.
- Missing cross-venue agreement is neutral and marked unobserved in the
  journal. Absorption is a trade-flow proxy.
- The portfolio risk budget caps new size. The daily drawdown and
  consecutive-loss keys are retained for configuration compatibility and do
  not block entries; the existing per-symbol loss cooldown is active.
- Liquidation is estimated using configured maintenance margin and observed
  price. On a gap through liquidation, the model charges reserved margin and
  exit fee. Funding and venue-specific liquidation tiers are outside the model.
- New sessions are required when moving from the reference configuration.

## Termux latency correction

A stopped-session diagnostic showed 60 latency rejections and one spread
rejection, with no fills. Book refresh now requests the order book once and
uses its midpoint for the price guard. Freshness uses the source timestamp
when supplied, and otherwise conservatively uses request duration. It ages
with time after receipt. The paper execution ceiling is 2000 ms, changed
from 200 ms to accommodate the Termux REST path. Actual request time and
source-age measurements are journaled with each guard rejection.

Local regression tests verify 250–350 ms REST operation, rejection beyond
the ceiling, stale source timestamps, and exact compatibility for restoring
a previous session whose only changed setting is the latency limit.
Successful device-side execution still needs confirmation after restart.
