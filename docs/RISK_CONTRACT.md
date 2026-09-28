# Risk Contract

The risk engine is the mandatory gate between signals and exposure-changing execution. A missing,
timed-out, unhealthy, or indeterminate risk result is a rejection.

## Owned decisions

Risk owns position sizing, per-trade/strategy/portfolio risk budgets, gross and net exposure,
leverage selection and limits, drawdown, correlation/concentration, daily-loss controls, liquidity
caps, and the kill switch. Strategies and execution may apply stricter safety bounds but never
weaker ones.

`RiskDecision` records a stable decision id, source signal id, instrument, UTC timestamp, approval,
reason code/text, quantity, notional, selected leverage, configured maximum leverage, risk budget,
stop price, and audit metadata. Rejections use zero exposure-increasing quantity/notional in the
serialized boundary message.

## Risk reference price

`PositionSizingRequest.entry_price` is strategy intent only. It is never used directly for
position sizing, exposure caps, leverage, margin, or liquidation-distance math. Risk instead
derives a `risk_reference_price` from the current market: the ask plus a configurable slippage
buffer for a BUY, the bid minus that buffer for a SELL (`RiskPolicy.reference_price_slippage_bps`).
All quantity/notional/leverage computation uses `risk_reference_price`.

`entry_price` is validated against `risk_reference_price` with a dynamic tolerance — never a
single fixed global basis-point constant — computed as the maximum of a configured floor
(`reference_price_min_tolerance_bps`), a multiple of the current spread
(`reference_price_spread_tolerance_multiplier`), and a multiple of the current volatility
(`reference_price_volatility_tolerance_multiplier`). Exceeding that tolerance rejects the request
with `ENTRY_PRICE_DEVIATION`. If the market data needed to compute the reference price is missing,
stale, invalid, or non-finite, the request is rejected fail-closed by the existing
data-quality/staleness/invalid-input checks before the reference price is ever computed.

Resolves `docs/OPEN_QUESTIONS.md` #23 (2026-09-28).

## Leverage

The system ceiling is 20x; each environment, venue, instrument, strategy, and account may configure
a lower limit. Selected leverage must not exceed the strictest applicable limit. It is the result of
risk budget, stop distance, quantity/notional, liquidation buffer, volatility, liquidity, open
exposure, correlation, fees/funding, and portfolio constraints. Confidence alone cannot choose it.

## Fail-closed rules

New exposure is rejected when data is stale/invalid, account state is unknown, reconciliation is
incomplete, the risk engine is degraded, required stops cannot be represented, spread/slippage
guards fail, or any loss/exposure/leverage limit is breached. Reduce-only requests are separately
verified so they can never increase absolute or directional exposure.

Every decision stores input event/configuration versions and machine-readable reason codes. Risk
budgets are reserved atomically before execution and released or adjusted from idempotent fill and
cancel events.

