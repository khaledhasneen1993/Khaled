# Khaled 3.0.2 — approved source baseline and readiness review, 2026-11-04

**Source-release status: approved current project baseline, as requested by the repository owner. Approval covers the repository source and offline tests. Live acceptance remains pending. `exchange.mode` remains `paper`; no Mainnet approval certificate was created.**

This revision incorporates the latest 3.0.0 functionality, removes obsolete duplicate TP/SL documentation and unused imports, and consolidates the review here. No signal, sizing, TP/SL, exposure, execution or rollout policy was changed by this cleanup. Runtime filenames and public interfaces are retained for compatibility, not as competing previous bot versions. No authenticated Testnet session, Telegram message, or Mainnet order was executed.

The environment has no configured Binance API credentials or Telegram credentials. No VPS/long-running trial was provisioned. Elapsed Testnet uptime, executed trade counts, observed slippage, Sharpe, drawdown and live exchange protocol acceptance are therefore unavailable, not zero or passed.

## Implemented layers

| Layer | Implementation and behavior |
|---|---|
| Live adapter | `exchange/live_exchange.py`, alongside PaperExchange; same current public method signatures. Environment-only credentials, HMAC over URL-encoded parameters, time synchronization, isolated 50× setup once per symbol, actual fills and current USDT account equity. One-way/single-asset account required. |
| Native protection | Futures Algo API reduce-only SL and TP. Stop rounding moves inward; TP rounding moves outward. New protective order is submitted, queried and durably recorded before cancelling the old order. Actual reported liquidation price is checked in addition to the existing approximate guard. |
| Order uncertainty | Unique `kh_<uuid>` IDs, fsynced intent before POST, query/adopt after failures, no blind resubmission after ambiguous timeouts/503. Confirmed rejected 429/418 requests can use fresh IDs, at most three submissions. Unknown outcomes latch the runtime. |
| KillSwitch | Stable daily starting equity, realized/unrealized drawdown, four consecutive losses, stale stream, sliding API error window, spread spikes. Persisted latch; separate watchdog gates entries while REST is slow. Market flatten/cancel attempts, durable events and alerts. Remaining protective orders are retained if flatten fails. |
| Request limits | Cold-start token bucket at 5 requests/sec, capacity 10, plus request-weight and exchange order-count buckets. Shared 429/418 cooldown and Retry-After handling. Read retries are separate from order adoption. Expected already-isolated/not-found/cancel races do not count as transport incidents. |
| Reconciliation | Account-wide position query, not just local/scanner symbols. Orphan/phantom/size/entry/side mismatches, unknown orders, missing protection; mismatch stops trading. Native completed exits are adopted without false phantom classification where verifiable. Dedicated account required. |
| Exposure | Max three positions, directional/gross limits and explicit correlated groups. Default basis is margin, not notional; see the conflict resolution below. |
| Funding | Uses actual nextFundingTime from Binance premiumIndex, rather than assuming every contract settles at fixed eight-hour boundaries. Closes paying positions above the configured threshold inside the warning window. |
| Slippage/accounting | Entry and exit expected/actual prices and adverse bps; short sign handled correctly. Actual USDT fees, realized PnL and fill quantity coverage checked. Partial closes aggregate all fills before final position net PnL. |
| Alerts/reports | Environment-only Telegram settings, queued bounded/split messages, startup delivery preflight, trade/security notifications and previous-day UTC report. Delivery failure is journaled/latched; network delivery cannot be guaranteed while offline. |
| Current equity | Sizing uses current equity, including unrealized losses. Base margin remains 10%, leverage 50× and max positions three. Mainnet allocation overlay begins at 1% of target size and risk budget. |
| Time Stop | After existing TP/SL checks, close at 45 minutes if favorable price change is below 0.2%. No change to the existing ATR/BE/trailing formulas. |
| Restart/shutdown | Mode-separated state, account process lock, fsynced atomic state, durable order intents/protection, startup reconciliation. Live shutdown attempts flattening and leaves a review latch. |
| Rollout | Evidence records monitored duration, uninterrupted duration, actual trades and equity samples, bound to code/config fingerprints. Mainnet requires 14 days, seven continuous days, metrics and explicit approval. Each scaling step requires separate reviewed evidence after 14 more monitored days, at most 2×, and applies on restart. |
| Key controls | Local operator attestation for dedicated account, Futures-only permission, no withdrawals, IP whitelist and rotation under 90 days. These controls are not claimed to have been verified or applied on the user's Binance account. |

## Intentional resolutions of inconsistent or obsolete examples

1. The repository's existing `create_order(self, symbol, type, side, amount, params=None)` and `set_leverage(self, leverage, symbol)` signatures differ from the sample pseudocode. They are retained exactly. Additive PaperExchange methods allow direct signature comparisons with LiveExchange.
2. Notional limits 0.3/0.5 times equity prohibit the original first position, which has approximately 5 times equity notional. Default `exposure.basis = margin` interprets those values as reserved margin limits. Selecting notional basis is supported and blocks the original size. Correlation and incident limits can reduce executed opportunities; no unchanged-count guarantee is made.
3. Mainnet requires the stronger 14-day condition; the separate seven-day requirement is enforced as continuous uptime. No local test replaces elapsed real time.
4. Binance's current documentation uses `demo-fapi.binance.com` and `demo-fstream.binance.com` for Testnet. These are configured separately from Mainnet.
5. Futures conditional orders use Algo endpoints. `PUT /fapi/v1/order` currently supports LIMIT modification, and Spot cancelReplace is not used for Futures stops. Create-confirm-before-cancel retains protection, but is not falsely described as a native atomic amendment.
6. An empty order query after a timeout does not conclusively prove the original POST failed. Ambiguous absence halts rather than generating a second potentially duplicate entry. Fresh IDs are used only after a definite rejected submission eligible for retry.
7. A bucket starting with burst tokens permits `(N-burst)/rate` elapsed time. This implementation starts empty, so the required 100-call minimum of `100/rate` is meaningful while idle periods can still accumulate burst capacity.
8. Manual review is required after a latch. A calendar-day change does not silently clear a Mainnet incident. Daily ordinary accounting rolls at UTC boundaries; the previous-day report is computed separately.
9. The original wallet sizing is deliberately changed to current equity as explicitly requested in P1.5. The mode itself is selected only through baseline; credentials, operator attestations and rollout evidence are prerequisites, not alternative mode switches.

## Verification performed

- `python -m pytest tests/test_live_layers.py -q`: **20/20 passed**.
- `python -m pytest -q`: **87 passed, 9 subtests passed**.
- HTTP/stateful exchange fakes exercise signed wire requests, 503 adoption without a second POST, ambiguous absence without resubmission, protective replacement ordering, keeping the old stop after replacement failure, full entry-to-TP flow, native SL adoption, partial-close accounting, failed-protection flattening, persistent latches, and rollout/operator gates.
- Existing public signatures were compared by AST against the prior commit and preserved. ATR, approximate liquidation, dynamic-stop and trade-journal math modules are byte-identical to the prior commit. Original cascade, orderbook detection, universe selection and scanner signal methods are unchanged.
- CLI help checks pass. `git diff --check` passes. Pattern-based secret audit finds no probable literal Binance/Telegram keys in tracked contents or reachable history. This is a heuristic audit, not proof of universal historical absence.
- Readiness tool reports selected mode `paper`, with Testnet and Mainnet evidence `not_run_or_unavailable`; it never creates an approval.

## Acceptance matrix

| Requested criterion | Result |
|---|---|
| Required live-layer tests 20/20 | Passed offline |
| Continuous Testnet ≥7 days without crash | Not run |
| Total Testnet ≥14 days before Mainnet | Not run; gate remains locked |
| Executed Testnet trades ≥100 | Unavailable |
| Normal-operation orphan/phantom count 0 | Unavailable; mocked cases tested |
| Technical KillSwitch count 0 | Unavailable; intentional fault tests pass |
| Mean entry slippage <5 bps | Unavailable |
| Testnet Sharpe ≥0.8 | Unavailable |
| Maximum drawdown <15% | Unavailable |
| Trades over 60 minutes <10% | Unavailable |
| Mainnet starts at 1% with explicit approval | Implemented guard/overlay; not activated |
| Reviewed doubling after each 14-day interval | Implemented guard; not activated or empirically qualified |
| Key permissions, IP restrictions, account isolation, key rotation | Must be verified by the operator on the actual account |
| Telegram startup/trade/security/daily reports on real service | Not configured or sent |

No artificial trades, returns, uptime or production approval were substituted for these missing results. Testnet metrics are accumulated from the running process. Minute equity returns are used for a rolling 100-sample annualized Sharpe, with the annualization factor configured in baseline. A trial should use a dedicated account without external cashflows/resets; the records are operator-controlled local evidence, not tamper-proof attestations.

## Remaining operational work

Configure Testnet and Telegram environment values on the actual persistent host, verify the operator policy, select testnet, and run the trial. Review native Algo behavior, filters/minimum order value, actual fee denomination, latency, rate limits, partial fills, outage recovery and all measured acceptance thresholds on that real service. Mainnet remains prohibited until this is complete and explicit approval is issued. No key, approval certificate or private account setting was created in this repository.

The paper model still does not reproduce actual liquidation or funding executions. The live adapter supports the market/conditional orders required by this bot and USDT-paid fee accounting; it is not a general multi-asset/hedge-mode trading platform. If the API is unreachable, immediate flattening cannot be guaranteed; native protection is retained where possible and entry remains latched. Small staged quantities may fall below Binance filters; size is never increased automatically to bypass a minimum.

## Primary API references consulted

- General API/Testnet endpoints and unknown 503 status: https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
- New/query/cancel Algo orders, account-wide orders, position risk, leverage/margin and LIMIT-only modification: https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

These references were read during implementation. Endpoint compatibility remains subject to actual Testnet verification.


## Retained exit-math evidence

The previous standalone TP/SL report has been removed because its wallet-sizing/no-time-stop description and 45-test count described an earlier revision. Its useful historical evidence remains in [historical_exit_validation.json](historical_exit_validation.json), with the reproducible checker [validate_exit_history.py](../tools/validate_exit_history.py).

The retained July–September 2026 BTC/ETH candle study checks 529,864 hypothetical initial long/short level calculations on 264,960 candles. It found zero violations of the approximate liquidation bounds, 3% planned risk including fees, or R:R ≥1.5. These are mathematical scenarios at a fixed hypothetical equity, not executed strategy trades or live qualification. They do not establish Sharpe, drawdown, trade counts, or actual broker safety. The raw source hashes and calculation results are kept for audit/regression value, not presented as a separate approved version.


## Local comparison launcher correction

Added tools/run_comparison_dashboard.py for the existing Ubuntu setup: Final at /root/cascade-git and Khaled at /root/Khaled. Local loopback UI operation does not require the cloud publisher token. The launcher extracts only pinned UI assets, patches source paths to current checkouts, installs both current dependency sets, creates unique state paths, and verifies 100 USDT/zero positions/zero fills before starting the server. It refuses non-paper modes and does not load historical strategy code. An offline integration check prepared actual Final and Khaled accounts and queried the local UI with worker autostart disabled: both reported balance 100 and zero fills. This check did not connect either bot to market streams. JavaScript syntax validation passed for the updated ATR settings display.
