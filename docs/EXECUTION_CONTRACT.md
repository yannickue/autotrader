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

