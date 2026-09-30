# Execution Contract

Execution is the only component allowed to call venue order APIs. It consumes a fresh,
risk-approved `ExecutionRequest`; it does not infer strategy intent or increase approved size.

## ExecutionRequest

Required fields are a stable request id, risk-decision id, instrument, UTC timestamp, side,
positive quantity, order type, optional/required limit price, reduce-only flag, unique client order
id, time in force, and versioned metadata. Limit orders require a limit price. Requests are rejected
when their risk decision is missing, stale, rejected, mismatched, or already consumed.

## Responsibilities

Execution owns actual order creation, market/limit selection, maker/taker policy, reduce-only,
protective stop placement, take-profit and trailing behavior, partial fills, cancellation,
cancel/replace, stale order/signal handling, duplicate protection, reconnect, venue reconciliation,
spread protection, and slippage protection.

## Fees and liquidity-role mapping

Every real (non-duplicate) fill is priced through `costs.engine.calculate_fill_fee` against a
required `VenueCostSchedule` (`PaperExecutionEngine.__init__`) and the resulting fee is recorded on
`Fill.fee`, which `Portfolio` already includes in realized PnL and equity. Liquidity role selects
the maker/taker rate: a MARKET order and a triggered protective STOP child are always TAKER (they
remove liquidity); a resting ENTRY limit crossed via a trade event and a TAKE_PROFIT child are
MAKER (they sat on the book); an externally reported fill of unknown origin (`report_fill`) is
conservatively TAKER.

The paper fill price already embeds spread and slippage (`ask*(1+slip)` / `bid*(1-slip)`), so only
the exchange fee/commission are ever debited as a real, additional cash cost — never spread or
slippage a second time. A separate, reporting-only round-trip `CostBreakdown` (via
`costs.engine.calculate_trade_costs`, exposed as `PaperTradingPipeline.metrics()
["cost_attributions"]`) is produced when a trade fully closes; it is never subtracted from
`TradeOutcome` or the portfolio ledger. Funding/swap cost is estimate-only in that breakdown — it is
never debited into realized PnL (`docs/OPEN_QUESTIONS.md` #25 Q-C1): the model's `floor(holding
/interval) * rate` approximation is a known simplification of how real perpetual funding is
actually paid (only at the funding timestamp).

`PaperExecutionEngine.fill_listener`, invoked synchronously with a `FillEvent` after every real
fill, is how a caller (the pipeline, or a later component) observes the actual fill price/fee
instead of inferring them from the quote-side reference price the request was built from.

`RiskEngine`'s daily-loss check (`AccountRiskState.realized_pnl_today`) is net of fees
(`portfolio.realized_pnl - portfolio.fees`) now that fees are real — Sprint 1 has no authoritative
UTC daily-reset boundary yet (`docs/OPEN_QUESTIONS.md` #13, still open), so both terms remain
all-time cumulative, consistent with each other.

## Persistence and crash recovery

`PaperTradingPipeline` persists to a `SQLiteStore` (`src/persistence`) at the end of every public
mutating call, in one transaction covering positions, orders, risk reservations, portfolio state,
risk's own halt state, execution's mode/halt_reason (a deliberately separate row from risk's halt
-- either can fire independently of the other), component state (execution's internal dedup maps,
pipeline counters, every open exit position), and that call's real fills together. This is the
single commit point: for this single-process paper engine, an event's effects either fully commit
or, if the process crashes first, did not happen at all from the system's own recorded point of
view -- there is no partial state.

`recover_pipeline()` reconstructs a pipeline from persisted state without trusting it blindly:
every persisted fill is replayed (true chronological order, never lexicographic `fill_id` order)
into a fresh portfolio and cross-checked against the persisted rows; any disagreement halts both
engines. A reservation whose orders are all terminal is released as an audit-logged orphan; a
non-terminal order with no matching reservation halts (possible unapproved exposure). Execution
never resumes `READY` on restart regardless of what was persisted (`HALTED` stays `HALTED`,
everything else forces `RECONCILING`) -- the only path back to `READY` is an explicit,
documented-as-weak paper self-check `reconcile()` call, since paper has no independent venue to
reconcile against for real.

## Idempotency and reconciliation

- `request_id` and `client_order_id` are stable across retries.
- Duplicate requests and order events are no-ops after the first accepted transition.
- Fills deduplicate by venue trade/fill id before position or PnL updates.
- Order state transitions are monotonic and append-only in the audit log.
- Startup/reconnect enters `RECONCILING`; venue open orders, fills, balances, and positions are
  compared with local state before new exposure is permitted.
- Unknown external orders or position differences halt new exposure and require an explicit policy
  or operator resolution.

Market orders still require price-band and maximum-slippage policies. Stops must be acknowledged or
an equivalent deterministic contingency must be active; otherwise the system reduces or halts
exposure according to the approved recovery policy.


## Outbound actions vs inbound venue events (C2.1 -- binding for the Nautilus adapter)

- **OUTBOUND** actions that can change broker exposure (submit, modify/replace, cancel-and-replace,
  any order-creating request) require runtime/reconciliation admission: RECONCILED for everything;
  additionally mode READY for new exposure; reduce-only is admissible in READY or HALTED but never
  while NOT_RECONCILED / RECONCILING / MISMATCH. Enforced at the final execution admission layer
  (`PaperExecutionEngine._admission_blocked`) and, independently, by the risk policy.
- **INBOUND** broker truth -- fills, cancellations, rejects, position reports, order reports -- is
  ALWAYS ingested, in RECONCILING, DEGRADED, HALTED and MISMATCH alike. Ignoring or deferring a
  broker event never makes the system safer; it desynchronizes local state from reality. Ingestion may
  itself raise a flag (unknown order -> HALT + MISMATCH; overfill -> HALT) but never drops the event.
- Terminology in `PaperExecutionEngine`: `report_fill()` is INBOUND truth and bypasses the admission
  deferral (`_apply_fill(inbound=True)`). `on_trade()`/`on_time()` matching of a resting order is the
  paper venue's OWN simulated decision, the analogue of an outbound action, and may be deferred while
  the engine is unreconciled/halted. There is no such thing in a real venue: with Nautilus the venue
  decides fills, and every fill it reports is inbound.
- Inbound fills are booked AS REPORTED (no venue-like clamp). If a reported reduce-only fill exceeds the
  locally reducible position it is booked in full and then the engine HALTs as RECONCILIATION_MISMATCH.
  If an inbound fill cannot be booked (order/decision cap, exception) the engine becomes MISMATCH and the
  fill is NOT marked seen, so a re-delivery is never silently discarded as a duplicate.
- Creating or resizing protective (reduce-only) child orders is an OUTBOUND action: while the engine is
  not RECONCILED it is deferred (remembered in the checkpoint) and flushed by the next successful
  `reconcile()`, if the position is still open on the same side. The inbound fill itself is booked at once.
- Simulated fills of RESTING reduce-only/protective orders are the paper stand-in for broker-side stops:
  they only shrink exposure and mirror what a real venue does regardless of our state, so they are
  deliberately not deferred (`test_resting_protective_stop_still_fills_when_state_is_unreconciled`).
  Simulated fills of non-reduce-only resting orders remain deferrable.
- Not yet modeled in the paper engine (adapter work, C5): inbound cancel/reject/order/position
  reports. The invariant above applies to them when added.
- An INTERNAL_ERROR halt latches an internal-inconsistency flag: `reconcile()` compares only open-order
  ids and position quantities and cannot prove that order/decision accounting is intact after an exception
  interrupted a multi-step mutation, so while latched it refuses to return to RECONCILED (MISMATCH). Only
  restoring from durable state (`import_checkpoint`, i.e. `recover_pipeline`) clears it.

## Amendment 2026-09-30 (audit H2): emergency reduce-only close in any reconciliation state
The Nautilus/MT5 adapter admits a reduce-only close in ANY reconciliation/runtime state when, in the same lane call, the position
was read from the broker, its magic is ours, the order side is opposite, volume <= position volume and it is closed by ticket
(so an own position that lost its stop can always be flattened). It cannot increase exposure. New exposure and loosening/removing
protection stay blocked unless RECONCILED (+READY for new exposure). The retired paper engine keeps the stricter rule (parity
scenario `reduce_only_admission_matrix` is EXPECTED_DIFFERENCE).
