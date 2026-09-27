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
23. Risk sizing (leverage cap, gross/net capacity, instrument notional, liquidity) is computed
    entirely from `request.entry_price`; only the spread check compares against market bid/ask.
    An entry price far from the market (found in independent review, 2026-09-27: SELL request
    `entry=10` vs. market `bid/ask=99/101`) is approved at a computed leverage far under the
    configured cap, while the real notional at actual fill price would exceed it. Two candidate
    resolutions were identified — reject when entry deviates from the executable market price
    (ask for BUY, bid for SELL) beyond a tolerance, or size from `max(entry, executable_price)`
    — but the tolerance value and reject-vs-clamp choice are a contract decision, not something to
    invent while fixing a bug. See DEVELOPMENT_LEDGER.md's "Independent review findings" item 8
    for the full repro and analysis. Sprint 1 leaves `request.entry_price` as the sole sizing
    input until this is decided.
