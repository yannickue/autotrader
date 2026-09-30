# Architecture Contract

## Scope and principles

V1 is a single-venue, paper-first, event-driven system for 20–100 liquid markets with long and
short support. It supports intraday/scalping and wider-horizon momentum research without imposing a
trade-count target. Capital-growth examples are stretch outcomes, never optimization objectives.

NautilusTrader is the primary event, trading, and execution framework. Python owns strategies and
policy; Rust-backed Nautilus components are preferred on latency-sensitive paths. VectorBT and
Optuna are research-only. Parquet is the historical-data baseline. PostgreSQL or Redis require a
measured durability, coordination, or latency need before introduction.

The production hot path is deterministic:

```text
venue data -> adapter -> MarketSnapshot -> features -> strategy -> Signal
    -> risk engine -> RiskDecision -> ExecutionRequest -> execution adapter
    -> order/fill events -> portfolio + recorder + monitoring
```

An LLM may assist offline research or operator workflows, but it is never a dependency of signal,
risk, execution, reconciliation, or kill-switch decisions. TradingView is an optional monitoring,
visualization, and authenticated alert input; it is never the sole execution path.

## Component boundaries

- `data`, `features`, `universe`: normalize venue events, assess quality/staleness, select markets.
- `signals`, `strategies`: emit opinions only. They have no broker credentials or order API access.
- `risk`: owns risk budgets, sizing, leverage, exposure, drawdown, correlation, daily loss controls,
  and the kill switch. No strategy can bypass it.
- `execution`, `adapters`: own order construction, order type, maker/taker choice, reduce-only,
  cancel/replace, duplicate protection, slippage and spread guards, reconnects, venue
  reconciliation, and fee/liquidity-role pricing (`src/costs`). Also mechanically places and
  manages a protective STOP order at the position's original invalidation level as a hard
  backstop, independent of the exit engine's own decisions.
- `exits`: a decision layer, like `strategies` -- it evaluates break-even/trailing/partial- and
  multi-stage-take-profit/momentum/liquidity/time/emergency exit rules from measurable
  position/market state and emits reduce-only intent (`ExitDecision`) that goes through `risk`
  exactly like any other exposure-reducing request, never calling `execution` directly. This
  resolves the `execution`-vs-`exits` "stops, profit-taking, trailing" ownership question
  (`docs/OPEN_QUESTIONS.md` #25 Q-X0): profit-taking/trailing/break-even DECISIONS belong to
  `exits`; the mechanical protective-stop backstop stays in `execution`. Neither module's other
  responsibilities were redesigned.
- `margin`: a standalone, PAPER-ONLY liquidation-safety approximation (`src/margin`) consumed by
  `risk` as one pre-approval check. It is explicitly not authoritative for any real venue -- a
  future broker adapter (e.g. ActivTrades/MT5) MUST be able to replace this module's estimate
  cleanly with real broker-reported margin/liquidation data without `risk`'s other invariants
  changing. `MarginEngine`'s own module docstring documents its formula and known omissions in
  full; every result it produces is labeled `is_estimate: True` and must never be treated as venue
  truth.
- `costs`: a standalone transaction cost model (`src/costs`) consumed by `execution` (real per-fill
  fees) and `pipeline` (reporting-only round-trip cost attribution). `VenueCostSchedule` is
  venue-specific configuration; a schedule built for one venue/asset class (e.g. the crypto-shaped
  defaults used in Sprint 1 testing) must never be silently reused for a different one (e.g. a CFD
  venue) -- see `docs/OPEN_QUESTIONS.md` #25 for the explicit-identifiability requirement carried
  forward into the CFD/MT5 work.
- `persistence`: a standalone SQLite-backed durable store (`src/persistence`) for positions,
  orders, fills, reservations, portfolio, reconciliation, and halt state. Not yet wired into the
  live pipeline/execution/risk state (tracked as the next integration slice).
- `portfolio`: derives positions and PnL from idempotent execution events.
- `monitoring`, `health`: record health, decisions, state transitions, metrics, clock drift,
  staleness, heartbeats, and operator alerts.
- `research`: backtests and optimization only; it is not importable by production execution code.

Hot path (updated for the exit engine): `market data → features → opportunity scanner (planned)
→ regime/strategy router (planned) → strategies → risk → execution → fill → portfolio → exits
→ risk (evaluate_reduce_only) → execution → fill → portfolio`. The exits loop repeats every tick
for as long as a position stays open; it never bypasses risk.

Boundary messages are immutable dataclasses using `Decimal` for price, quantity, notional, risk,
and leverage. UTC timestamps and stable identifiers are mandatory at adapter boundaries.

## Safety state machine

The runtime states are `STARTING`, `RECONCILING`, `READY`, `DEGRADED`, and `HALTED`. New exposure
is allowed only in `READY`. Reduce-only actions may be allowed in `DEGRADED` or `HALTED` when the
account state is known and the action demonstrably lowers exposure.

These conditions fail closed for new exposure: stale/invalid data, unknown account state,
unavailable risk engine, reconciliation failure, excessive spread, excessive expected slippage,
daily-loss breach, drawdown breach, or operator kill switch. Restart and reconnect always enter
`RECONCILING`; the venue remains the authority for live orders, fills, and positions.

## Version decision

Checked against the official PyPI release metadata on 2026-09-27:

- selected: `nautilus-trader==1.231.0`, stable, uploaded 2026-08-02;
- deferred: `2.0.0rc5`, release candidate, uploaded 2026-09-15;
- supported Python range from the selected package: `>=3.12,<3.15`.

Production bootstrap favors the latest non-yanked stable release. A 2.0 migration requires replay,
adapter, state-recovery, and performance regression evidence before adoption.

## V1 sequence

1. Contracts and deterministic boundary types.
2. One venue's recorded WebSocket data and Parquet normalization.
3. Deterministic replay, recorder, and portfolio reconstruction.
4. Risk engine and execution simulator with invariant/property tests.
5. Paper trading with reconciliation and chaos tests.
6. Three research families—momentum, breakout, pullback—promoted only after bias and cost gates.
7. Shadow mode, then an explicitly approved live-readiness review.

Not in V1: dashboards, deep learning, reinforcement learning, options, or multiple brokers.

## Sprint 1 development venue

- Public market data: Binance USD-M perpetuals; no private credentials.
- Execution: NautilusTrader Sandbox or a deterministic Nautilus-compatible simulator only.
- Simulated account: 500 USDT starting equity.
- Instrument identity and precision remain Binance-compatible.
- Live execution, exchange API keys, and real orders remain disabled.
- The 30x ceiling (ActivTrades permits 1:30; raised from the obsolete 20x, 2026-09-29) is a hard maximum, never a default or confidence-derived target. Actual leverage stays determined by risk budget, stop distance, volatility, spread, liquidity, exposure, drawdown, loss streak, account and instrument constraints.

## Open decisions

1. Production venue and account model beyond Sprint 1's Binance USD-M linear-perpetual paper
   boundary.
2. Canonical instrument identifier and contract-size normalization.
3. Market-data staleness thresholds by feed and strategy horizon.
4. Portfolio exposure, correlation, daily-loss, and drawdown defaults.
5. Durable event-log technology after throughput and recovery measurements.
6. Execution latency/error budgets and clock-synchronization requirements.
7. Authentication and replay protection for optional TradingView alerts.
8. Criteria and target date for evaluating NautilusTrader 2.0.

## Decision 2026-09-30: demo OpportunityEngine may import FROZEN alpha kernels only
`src/demo/opportunity/**` (signal layer, not execution) reuses the causal V2 family generators. Allowed imports:
`alpha.families.*`, `alpha.common.market_data`, `alpha.fast.sim`, `alpha.session`. Search / optimisation / ML research code
(discovery, formula, rawscan, metalabel, growth, Optuna/DEAP) is forbidden there, and no execution / risk / adapter / persistence /
`demo.execution` module may import `alpha` (enforced by `tests/unit/alpha/test_protocol_metrics.py`). The hot path stays deterministic
and needs no AI agent; specs are frozen (`production_spec_v1.json`, strategy hash recorded on every snapshot).

## Decision 2026-09-30: no one-position rule; funnel and lifecycle (Lane I)
The opportunity policy and the runner no longer reject because a position exists (`ONE_POSITION_PER_INSTRUMENT` removed; `position_open` is
accepted and ignored for API compatibility). Broker netting is a broker representation only; the stack classifies same-symbol add-on /
opposite-side as TEMPORARY limitations, which the rejection funnel counts separately with `otherwise_valid_blocked`. `PolicyConfig.min_space_r`
defaults to `0.0` (exact simulator behaviour); candidate-carried `min_space_r` stays and is classified LEGACY_ARBITRARY; `ENTRY_OVERSHOT`
(entry already crossed) is STRUCTURAL. Quality inputs are logged only and have no reject code. `DemoStore` gained `risk_detail`, `tca_records`
and the non-terminal `IN_DOUBT` intent state (all backward compatible). Open: shadow mode does not run the stack risk gate, so the funnel's stack
part is empty in shadow; add-on execution (`ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED`) is the largest known trade-count limiter.
