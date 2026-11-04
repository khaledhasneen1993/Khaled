# Khaled TP/SL implementation and validation — 2026-11-04

**Status: implementation verified; full strategy performance acceptance is NOT established.**

Only Khaled is modified. Source baseline: Final commit `d200a07ba26592fadfb5ce1dcc1a5d8f42b89a57`, copied to Khaled commit `497e820c51e175a8fcb4766298c49f1fa6060439`. No trading process, dashboard publisher or real order was started.

## Implemented

ATR-based entry TP/SL, approximate isolated-margin liquidation bounds, account-risk stop clipping, breakeven, monotone trailing, persistent position metadata, and complete closure journal in `trades[].exit_detail`. Entry detection, volume spike, top-20 orderbook filter, scanner, cooldown, wallet-based sizing, 50× leverage and three-position limit are preserved. Existing public method signatures are unchanged; an AST comparison confirmed original detection, book, sizing and watcher methods, and a byte comparison confirmed `continuous_scanner.py` is unchanged.

All new configurable parameters come from `baseline.json`. The original baseline fields and constants are retained for compatibility; the active exit configuration is additionally included in persisted settings. Existing dictionaries remain JSON dictionaries; `Position` adds attribute access for stop management. Existing outer trade reasons remain `TP/SL`; the detailed journal uses the required `take_profit/stop_loss` and adds `exit_attribution` for breakeven/trailing.

## Necessary resolutions of conflicting requirements

1. **0.8% minimum stop versus 3% equity loss:** 10% wallet margin × 50 leverage = about 5× equity notional. A 0.8% price stop loses 4% equity before fees. The hard 3% planned-risk ceiling therefore takes priority over the ATR minimum floor. At the default 0.05% fee per fill and flat equity, the planned stop is approximately 0.50025% for longs or 0.49975% for shorts. Neither leverage nor position size is reduced. Gap executions can exceed 3%; a stop does not guarantee a fill at its trigger.
2. **Short liquidation rounding:** the supplied additive cap of 1.3% at 50× does not by itself satisfy the supplied strict multiplicative short check. Both bounds are enforced; the short-only multiplicative bound is slightly tighter. Floating-point prices are rounded inward.
3. **Exit-reason distributions:** the required journal has only two `exit_reason` values, whereas the performance target names four categories. The detailed reason remains the requested two-value enum. `exit_attribution` supplies the four-way analytical split without adding an exit mechanism.
4. **Data outage without filtering entries:** ATR reuses scanner histories, fetches candles when needed, then falls back to last valid ATR or `atr.fallback_atr_pct`. The source is journaled. Insufficient ATR history does not filter an otherwise eligible signal.
5. **Breakeven fees:** the configured 0.1% offset is increased only if necessary to cover both actual fill fees algebraically. Breakeven/trailing only adjust the stop; `manage_exits(self)` performs closes and gives SL precedence.

## Automated verification

Commands:

```bash
python -m pytest tests/test_tp_sl.py -q
python -m pytest -q
python run_paper_live.py --help
git diff --check
```

Results: **13/13 required TP/SL tests passed; 44 total tests and 11 unittest subtests passed.** Integration coverage includes both sides, actual fill anchoring, both entry/exit fees in planned account risk, ATR outage without rejecting entries, resume of armed stops, journal persistence, legacy-state upgrade, unchanged three-position sizing, and honest gap-fill loss reporting. The randomized required test covers 500 entry/ATR scenarios on each side across the five requested leverages.

## Historical data verification (not a strategy backtest)

Original Binance USDT-M monthly one-minute candle archives were downloaded for BTCUSDT and ETHUSDT, July–September 2026. Each symbol contains 132,480 contiguous candles (92 days); no gaps were found. Source URLs and SHA-256 hashes are recorded in [historical_exit_validation.json](historical_exit_validation.json). October monthly data was also inspected but has a missing 2026-10-28 for both symbols; the daily archives returned 404, so that incomplete month was excluded from the final validation period.

For each candle after ATR warmup, the validator computes hypothetical initial long and short levels at the unchanged baseline notional, and checks liquidation bounds, planned account risk including both fees, and R:R. These are **calculation scenarios, not executed trades**; no equity curve or trade performance is inferred.

| Historical calculation metric | BTCUSDT | ETHUSDT |
|---|---:|---:|
| Candles | 132,480 | 132,480 |
| Long + short level scenarios | 264,932 | 264,932 |
| Violations of approximate liquidation bounds | 0 | 0 |
| Violations of 3% planned equity risk including fees | 0 | 0 |
| R:R below 1.5 | 0 | 0 |
| Minimum R:R | 1.5 | 1.5 |
| Mean scenario R:R | 1.500739 | 1.503570 |
| Scenarios initially capped by liquidation bound | 15 (0.005662%) | 67 (0.025290%) |
| Scenarios capped further by equity risk | 100% | 100% |

Combined: 529,864 level scenarios, mean scenario R:R 1.502154. These fractions **must not be reported as the stop-capped fraction of strategy trades**. Parameters were not fitted to these scenario counts.

Reproduce with the original archives:

```bash
python tools/validate_exit_history.py --data-dir /path/to/archives --months 2026-07 2026-08 2026-09 --output /tmp/exit_validation.json
```

## Requested performance acceptance

| Criterion | Verified outcome |
|---|---|
| Executed trade count after ≥ before | Not established. Signal/sizing code preserved and entry integration tests pass; no historical execution comparison available. |
| Trade stop_capped fraction <20% | Not established for actual strategy trades. Scenario results above are not a substitute. |
| Average rr_planned ≥1.5 | Enforced by construction and verified in tests/history scenarios; strategy-trade average unavailable. |
| Trades remaining >60 minutes <20% | Not established. No time stop was added. |
| TP 25–45%, SL 15–35%, rest BE/trailing | Not established. Journal supports both reason and attribution. |
| Maximum drawdown <20% | Not established. No valid strategy equity curve available. |
| Rolling-100 Sharpe ≥0.8 | Not established. No valid strategy returns, rolling sample or annualization established. |
| Required tests 13/13 | Passed. |

**Blocker:** minute-close candles do not contain the top-20 orderbook imbalance or the evolving intrabar candle events used by `ContinuousScanner`. Fabricating imbalance, ignoring that filter, or using final candle volumes for earlier intrabar entries changes the original entry strategy. A full faithful backtest needs timestamped candle update/trade events, top-20 bid/ask history, and consistent ticker/fill observations; the dynamic universe also needs historical contract eligibility and quote volumes. None are provided in this repository or the downloaded kline archives. No trade count, drawdown, Sharpe or profitability result has been fabricated, and no parameter tuning against these missing metrics has been claimed.

## Model limits

The requested liquidation formula is an approximation with a configurable fixed MMR, not a reproduction of Binance risk tiers. Binance liquidation is triggered by Mark Price and depends on margin requirements: https://www.binance.com/en/support/faq/detail/360033525271 . This paper model still does not simulate liquidation events, funding or exchange-native stops. Exit checks remain on the existing schedule; no bound on gap losses is guaranteed. Inherited dashboard identity/configuration was deliberately left outside this TP/SL update.
