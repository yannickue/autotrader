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

## Margin and liquidation safety

After sizing (which needs quantity/notional) and before the reservation is written, every approval
also passes a margin/liquidation-safety check (`MarginEngine.evaluate_stop_safety`, `src/margin/`):
the request's stop must clear the estimated liquidation price, buffered by a configured venue
uncertainty margin, by at least a configured minimum distance. Both the buffer
(`RiskPolicy.liquidation_uncertainty_buffer_bps`) and the minimum distance
(`RiskPolicy.min_stop_liquidation_distance_bps`) are required, fail-closed configuration — never
optional/None-means-disabled. The liquidation estimate uses `risk_reference_price`, never
`request.entry_price`, consistent with "Risk reference price" above.

The leverage fed into the liquidation estimate is `account_gross_leverage_after` — the account-wide
cross-margin worst case after this fill — not per-decision leverage and not the strictest
configured cap (`docs/OPEN_QUESTIONS.md` #25, Q-M1). Per-instrument maintenance-margin-rate
assumptions live on `InstrumentRiskLimits.maintenance_margin_rate`.

A rejection here (`MARGIN_STOP_TOO_CLOSE_TO_LIQUIDATION` or `MARGIN_STOP_BEYOND_LIQUIDATION`)
leaves no reservation behind. An approval's metadata records
`liquidation_estimate_buffered`/`stop_liquidation_distance_bps` and always marks
`liquidation_is_estimate: True` — this is a simplified isolated-margin approximation, never an
exact venue computation (see `src/margin/engine.py` module docstring for the exact formula and its
known omissions: fees, funding accrual, mark-price basis, tiered margin schedules, ADL/insurance
fund mechanics).

`_stop_distance` also validates the stop is on the correct side of `risk_reference_price`, not just
`request.entry_price` — a stop between the two would otherwise pass validation yet trigger
immediately once sizing/margin math switches to the reference price.

Resolves `docs/OPEN_QUESTIONS.md` #25 Q-M1 (2026-09-28).

## Reduce-only reservations

`RiskEngine.evaluate_reduce_only()` reserves quantity per decision id exactly like `evaluate()`
does, released via `RiskEngine.release(decision_id)`. The exit engine (`src/exits`) is the primary
caller: it emits `ExitDecision`s as reduce-only requests and calls `ExitEngine.notify_terminal()`
on every terminal execution outcome (fill/cancel/reject) for that request, which forwards to
`release()` (idempotent, safe to call more than once). This resolves `docs/OPEN_QUESTIONS.md` #24.

Reduce-only is allowed during a HALT unless the halt itself means account state is unreliable
(`RECONCILIATION_MISMATCH`, `UNKNOWN_ORDER`, or an `INTERNAL_ERROR` halt of ambiguous origin) --
see `docs/OPEN_QUESTIONS.md` #25 Q-X2. A halt never itself triggers a flatten; existing positions
stay open under normal exit management (Q-X3). One position per instrument is enforced at the
pipeline level (Q-X1): no new entry while a position is open on that instrument, and an opposing
signal is a reversal trigger for the exit engine, never a same-tick flip.

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

