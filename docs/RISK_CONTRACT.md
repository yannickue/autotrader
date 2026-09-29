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

The system ceiling is 30x (a ceiling, not a target; was 20x until 2026-09-29); each environment, venue, instrument, strategy, and account may configure
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


## Daily PnL window (corrected 2026-09-29)

`AccountRiskState.realized_pnl_today` is the realized PnL **minus fees** of fills inside the current
*trading day* only (`risk.trading_day`). Unrealized PnL is added separately by the daily-loss gate.
Fees are charged on the day they are paid.

- Old behavior: the field held the all-time cumulative realized PnL minus all-time fees, so an old
  loss kept tripping (or an old profit masked) the daily-loss gate. There was no day boundary.
- New behavior: `TradingDayPolicy(timezone, rollover_time, calibration)`; default UTC midnight,
  explicitly `PENDING_BROKER_CALIBRATION` (ActivTrades' real rollover is unknown and is not invented).
  The pipeline supplies `pnl_window_start`; the risk policy rejects (`INVALID_INPUT`) a window that
  starts in the future or more than 25 h ago, so an all-time figure cannot be passed as "today".
- Fills with an unknown or future timestamp contribute only their LOSS (`min(net, 0)`): never dropped,
  never able to mask a real loss today with a profit. A portfolio checkpoint written before this fix
  is imported as one unknown-timestamp entry (all-time loss counted, all-time profit ignored). A
  checkpoint whose per-day ledger does not sum to `realized_pnl`/`fees` is rejected as corrupt.

## Reconciliation state (corrected 2026-09-29)

`ReconciliationState` = `NOT_RECONCILED | RECONCILING | RECONCILED | MISMATCH`, independent of the
runtime mode. `EngineMode.READY` is a permission mode and is never evidence of reconciliation.

- Old behavior: the pipeline mapped `READY` (and `HALTED` with a "reliable" halt-code string it parsed
  out of `halt_reason`) to `reconciled=True`; persistence wrote `last_reconciled_at=now` whenever
  READY; after a restart `recover_pipeline` "reconciled" against a snapshot built from the engine's
  own state; a failed recovery still counted as reconciled for reduce-only.
- New behavior: only `PaperExecutionEngine.reconcile()` (an explicit comparison) can produce
  `RECONCILED`; it records `reconciliation_source` (`VENUE_SNAPSHOT` or, for paper's own
  consistency check, `PAPER_SELF_CHECK`) and the real `last_reconciled_at`. Engines and restarts begin
  `NOT_RECONCILED`; a mismatch or an UNKNOWN_ORDER / INTERNAL_ERROR halt or failed recovery is
  `MISMATCH`. Every non-RECONCILED state blocks new exposure AND reduce-only (`ACCOUNT_UNRECONCILED`,
  text names the state). An exception inside `reconcile()` halts (INTERNAL_ERROR -> MISMATCH); the
  engine never stays READY after a failed comparison. A halt that did not invalidate state (e.g. OVERFILL) leaves the previous
  RECONCILED outcome intact, so the existing Q-X2 rule "reduce-only stays allowed during such a halt"
  is preserved. The only stricter change: a restart into a persisted HALTED engine is now
  NOT_RECONCILED (reduce-only blocked) until `resume_after_reconcile` succeeds.

## Pure policy boundary (slice C2)

`risk.policy.RiskPolicyEvaluator` + `risk.sizing.PositionSizer` are pure: inputs in, a
`ProposedOrderIntent | PolicyRejection` out. The halt latch, the decision cache and the reservation
ledger stay in the legacy `RiskEngine` and reach the pure layer only as explicit inputs
(`latched_halt`, `PendingExposure`, `reserved_reduce_only_quantity`). Gate order, reason codes and
arithmetic are unchanged (`tests/unit/risk/test_policy_purity_parity.py`).

### Post-fix review notes (2026-09-29)

- Execution admission (C2.1, supersedes the earlier note): `PaperExecutionEngine` admits OUTBOUND
  orders only when `RECONCILED`. New exposure additionally needs mode READY. Reduce-only is admissible
  in READY or HALTED -- HALTED does not by itself prohibit an exit -- but never in NOT_RECONCILED,
  RECONCILING or MISMATCH (so never after UNKNOWN_ORDER, INTERNAL_ERROR, a mismatch halt or a failed
  recovery). The same state is also checked by the risk policy: defense in depth. `EngineMode` has no
  DEGRADED; the risk layer's DEGRADED runtime mode does not block reduce-only.
  The table: READY+RECONCILED exit allowed; HALTED+RECONCILED exit allowed; NOT_RECONCILED /
  RECONCILING / MISMATCH exit blocked.
- `RealizedPnlEntry` validates at construction (UTC-aware timestamp, finite amounts); a naive fill
  timestamp is rejected before any portfolio mutation; `Portfolio.import_state` is atomic.
- Persistence (C2.1): the reconciliation record now stores `state`, `source` and the time of the last
  successful comparison (`last_reconciled_at`); state format version 3. A `paper_self_check` record
  never grants venue authority (`ReconciliationStateRecord.grants_venue_authority`,
  `PaperExecutionEngine.venue_reconciled`); a restart always begins NOT_RECONCILED and a stored record
  is an audit trail, never an authority. A format-2 database is refused by `recover()`.
- Known, deferred (Medium): a rollover time that falls in a DST gap/overlap of the configured zone is resolved by
  `zoneinfo` defaults (fold=0) -- pick a rollover time outside DST transitions until the broker's
  real rollover is calibrated.
