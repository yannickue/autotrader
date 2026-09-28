# Open Questions

This file records contract and integration questions that must not be answered by silently
redesigning an owned boundary. Sprint agents report questions to the Integration/Lead Agent, who
records and resolves them here.

## Sprint 1

1. Which exact Binance USD-M perpetual symbol subset and quote-volume window should seed the first
   20–100 instrument universe?
2. Which NautilusTrader Sandbox capabilities are available for L2 queue position and
   trade-triggered fills in version `1.231.0`, and which behaviors need a deterministic local
   simulator until an adapter is selected?
3. What production defaults should be approved for per-trade risk, gross/net exposure, daily loss,
   drawdown, consecutive losses, spread, and slippage? Sprint 1 must keep these configurable and use
   conservative test-only values.
4. Which mark-price and funding-rate source is authoritative for paper PnL and funding costs?
5. Should signal fusion select one winner, permit non-conflicting multi-strategy exposure, or emit a
   separate composite signal contract after calibration data exists?
6. Which versioned Parquet schema and partition retention policy should become the durable V1 data
   contract after replay measurements?
7. What exact reconnect/reconciliation policy should apply when the simulated portfolio and venue
   market-data session disagree on instrument availability?
8. The data contract defines duplicate source keys as idempotent but does not define conflicting
   payloads for the same key. Sprint 1 rejects conflicting duplicates fail-closed and accepts only
   logically identical duplicates; the durable event-log contract must formalize this behavior.
9. No canonical OHLC/bar feature message exists. Sprint 1 breakout and pullback strategies use
   rolling `MarketSnapshot.last` extrema; a later contract must decide whether bars become a shared
   boundary type.
10. `Signal.expected_move` has no explicit unit field. Sprint 1 uses Decimal return fractions and
    records feature units in metadata; the signal contract should make units machine-readable.
11. Data-quality/staleness gating ownership at the strategy boundary is not explicit. Sprint 1
    enforces it fail-closed in data/risk integration, while strategies validate UTC ordering and
    homogeneous instruments.
12. `RiskDecision.leverage` has no separate venue-setting field. Sprint 1 defines it as effective
    exposure divided by equity; a later venue adapter must model any configured exchange leverage
    separately.
13. The daily-loss contract still needs an authoritative UTC reset boundary and persisted source.
    Sprint 1 includes realized and current unrealized PnL and rejects at the configured boundary.
14. Standard units and estimators for available liquidity and volatility are not yet contractual.
    Sprint 1 uses explicit quote-notional liquidity and permits only a maximum-volatility guard; it
    does not invent a volatility sizing multiplier.
15. Atomic risk-reservation persistence and ownership across process restarts remain undecided.
    Sprint 1 provides an in-process idempotent reservation ledger and includes reserved exposure in
    capacity checks, but does not claim durable multi-process coordination.
16. Reduce-only actions do not naturally have a strategy `signal_id`. Sprint 1 uses a dedicated
    reduce-only request and a stable synthetic source identifier rather than inferring reduction
    from direction or metadata.
17. New exposure requires `DataQuality.LIVE` in Sprint 1. Whether `DELAYED` data may ever open
    exposure requires an explicit later policy decision.
18. Execution time-in-force compatibility is not explicit in the shared contract. Sprint 1 uses
    GTC for resting limits and IOC/FOK for immediately executable orders; any broader matrix must
    be specified before adding a venue adapter.
19. Cancel/replace client-ID semantics and handling of legitimate late fills after cancel need a
    durable event-log contract. Sprint 1 uses a new replacement client ID, links it to the original,
    and books each late fill at most once without regressing terminal order status.
20. Paper stop, take-profit, and trailing triggers need one authoritative event source. Sprint 1
    accepts explicit trade/mark events only; quote or candle touches never count as fills or
    triggers.
21. NautilusTrader `1.231.0` Sandbox does not expose a sufficient reconciliation source for this
    sprint. Sprint 1 therefore uses a deterministic local execution core and checkpoint interface;
    direct `SimulatedExchange` integration with recorded L2/trade data remains a later task.
22. The durable checkpoint/event-journal storage backend is not selected. Sprint 1 can prove
    deterministic export/import but must not claim crash durability or external exactly-once
    guarantees.
23. **RESOLVED 2026-09-28.** Risk sizing (leverage cap, gross/net capacity, instrument notional,
    liquidity) was computed entirely from `request.entry_price`; only the spread check compared
    against market bid/ask. An entry price far from the market (found in independent review,
    2026-09-27: SELL request `entry=10` vs. market `bid/ask=99/101`) was approved at a computed
    leverage far under the configured cap, while the real notional at actual fill price would
    exceed it. User-decided policy: introduce a canonical `risk_reference_price` (ask + slippage
    buffer for BUY, bid - slippage buffer for SELL) that becomes authoritative for sizing,
    exposure, leverage, margin, and liquidation-distance math; `request.entry_price` remains
    strategy intent only and is validated against `risk_reference_price` with a dynamic tolerance
    (floor, spread multiple, volatility multiple — never one fixed global bps constant),
    rejecting with `ENTRY_PRICE_DEVIATION` beyond tolerance and fail-closed on missing/stale/
    invalid market data. Implemented in `src/risk/engine.py` (`RiskEngine._reference_price`) and
    `src/risk/models.py` (`RiskPolicy.reference_price_*` fields); see `docs/RISK_CONTRACT.md`
    "Risk reference price" and regression tests in `tests/unit/risk/test_engine.py`
    (`test_entry_price_far_from_market_rejects_*`).
24. `RiskEngine.evaluate_reduce_only()` (added to fix a reduce-only-can-flip-past-flat bug,
    2026-09-27) reserves quantity per decision_id the same way `evaluate()` already did, released
    via `RiskEngine.release(decision_id)`. For the main `evaluate()` path, `src/pipeline/paper.py`
    already calls `release()` on every terminal execution outcome (fill/cancel/reject). No
    equivalent caller exists for `evaluate_reduce_only()` at all — it currently has zero callers
    anywhere in `src/` (grep-verified), since Sprint 1 has no exit engine yet (strategies emit
    entries only, per `SPRINT1_FINAL_REPORT.md`'s "Remaining stubs"). Confirmed by independent
    review, 2026-09-27: calling `evaluate_reduce_only()` directly and never releasing it leaves a
    stale reservation that can block a later, legitimate reduce-only request for the same
    instrument. This is not a bug in currently-wired code (there is no wiring yet) but a
    **contract requirement for whoever builds the exit engine**: it MUST call
    `risk_engine.release(decision.decision_id)` on every terminal outcome of a reduce-only order,
    mirroring exactly what `src/pipeline/paper.py`'s entry path already does. Not implemented now
    because there is no real caller to wire it into yet — do not invent one.
25. **RESOLVED 2026-09-28 (Phase A integration policy decisions).** An AUDITOR (Opus) integration
    plan for wiring `src/costs`, `src/exits`, `src/persistence`, `src/margin` into
    `src/pipeline/paper.py` / `src/risk/engine.py` / `src/execution/paper.py` surfaced six genuine
    policy decisions, resolved by the user as follows (implementation follows in sequential
    slices; see DEVELOPMENT_LEDGER.md):
    - **Margin leverage input (Q-M1):** the liquidation-safety check
      (`MarginEngine.evaluate_stop_safety`) uses `account_gross_leverage_after` (the account-wide
      cross-margin worst case already computed in `RiskEngine._evaluate_inner`), not
      per-decision leverage or the strictest configured cap.
    - **Pyramiding/reversal (Q-X1):** one open position per instrument. No new entry while a
      position is open on that instrument; an opposite-direction signal is a reversal trigger for
      the exit engine (closes the position) and never flips it through zero in the same tick.
    - **Reduce-only during HALT (Q-X2):** allowed, except when the halt itself means account
      state is unreliable (a reconciliation mismatch or an unrecognized/unknown order) — matches
      `docs/ARCHITECTURE.md`'s existing "reduce-only allowed in HALTED/DEGRADED when account state
      is known" language.
    - **Halt/kill-switch flattening (Q-X3):** no automatic flattening. A halt means
      NO_NEW_EXPOSURE only; existing positions stay open under normal exit management. The exit
      engine's forced-emergency-close path stays reserved for stale/invalid market data, not for
      every halt.
    - **Funding cost (Q-C1):** reported/estimated in `CostBreakdown` only, never debited into
      realized PnL — the `floor(holding/interval) * rate` approximation is known-imprecise
      relative to how real perpetual funding is actually paid (only at the funding timestamp).
    - **Exit engine vs. execution ownership (Q-X0):** the exit engine is a decision layer (like a
      strategy) that emits reduce-only intent through risk; execution keeps mechanically managing
      a protective STOP order at the original invalidation level as a hard backstop. Neither
      module's existing responsibilities are redesigned.

26. `PaperExecutionEngine.cancel_replace()`'s `new_price` parameter lets a caller reprice a
    resting limit order to anything (no bounds check), while the ORIGINAL risk-approved leverage/
    notional/gross-net-exposure caps were computed against the ORIGINAL `entry_price`. Confirmed
    by independent review, 2026-09-27: repricing a small approved order to a price orders of
    magnitude away, then letting a crossing trade fill it, produces real notional far beyond what
    risk approved — `cancel_replace()` only bounds *quantity* (`CANCEL_REPLACE_EXCEEDS_REMAINING`),
    never price/notional. What the correct bound should be is undecided: reject any repriced fill
    whose notional would exceed what was originally approved for that decision (mirroring the new
    decision-level fill-quantity cap), require the caller to obtain a fresh risk decision for any
    non-trivial reprice, or bound the allowed price deviation directly. Not implemented now — this
    is a contract decision (which bound, what tolerance), not something to guess while fixing a
    bug. See `DEVELOPMENT_LEDGER.md`'s "Independent review findings" (final Codex review,
    2026-09-27) for the full repro.
