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

