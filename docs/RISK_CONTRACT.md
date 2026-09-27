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

