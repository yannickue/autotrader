# DEMO Trader (ActivTrades DEMO only) — design and lane contracts

Scope: ActivTrades DEMO account only. `scripts/live.py` stays inert. Broker leverage hard cap 30x (never a target;
size is risk-based). No AI/LLM/Optuna/DEAP/research runner in the hot path; the runner is deterministic and needs no agent.
V2 result stands: NO proven edge. Demo signals are *discovery data*, never "a profitable strategy".

## Pipeline
MARKET (MT5 bars/quotes) -> OpportunityEngine (causal, V2 family generators) -> OpportunitySnapshot (persisted BEFORE decision)
-> Decision (static demo policy = champion; accepted|rejected + reasons; challengers only SHADOW) -> [accepted] TradeIntent
-> Risk (existing NautilusRiskBridge/RiskPolicy; 1% equity risk start, <=2% after stable canary) -> Execution (Nautilus MT5 adapter,
broker stop mandatory, reduce-only exits) -> Outcome -> TradeLearningRecord / CounterfactualLabel (rejected, post-horizon) -> dataset.

## Phases (addendum 1)
Every record carries `phase` = DISCOVERY | FROZEN. Discovery data may train models and tune rules and is then NOT a clean holdout.
A later FROZEN phase freezes champion+policy (no parameter change, no retraining in that window). Historical holdout >= 2026-09-01 untouched.

## Contracts
`src/demo/contracts.py` (Lead-owned): OpportunitySnapshot, Decision, TradeIntent, RiskRecord, ExecutionRecord, OutcomeRecord,
CounterfactualLabel, TradeLearningRecord, ClockCheck. Snapshot has no outcome fields (PRE-DECISION only).

## Simulator parity (addendum 4) — executor + opportunity policy MUST implement
target_crossed_at_fill (skip if actual fill already beyond target), min_space_r evaluated on ACTUAL fill/ask-bid, stale/already-crossed
entry (price ran through entry beyond tolerance -> reject), structural invalidation (price beyond invalidation before fill -> cancel),
entry only inside SimWindow, forced flat at `flat_min` (reduce-only close), one position per instrument.

## Clock verification (addendum 3) — per market before DEMO AUTO
UTC -> market tz (MarketSpec.calendar.tz) -> DST -> local trading minute -> broker session/calendar -> SimWindow.
`ClockCheck` is stored with every snapshot. Calendars marked `provisional` stay PROVISIONAL in records until broker-confirmed.

## Costs (addendum 5)
Always record spread at send, slippage (fill vs intended), commission (deal.commission), swap (deal.swap for overnight).
`ExecutionRecord.cost_status` = "provisional" until commission/swap are verified from real deals.

## Modules / lanes / ownership
- Lane A (Lead): contracts, spec, runner contract, ledger, integration, merges.
- Lane B (`src/demo/opportunity/`, `tests/unit/demo/opportunity/`): BarSource protocol, live causal generation from
  `alpha.families` on a rolling M5 window for GER40/NAS100/SPX500/XAUUSD/EURUSD, geometry+policy gates, snapshot builder, dedupe.
- Lane C (`src/nautilus_mt5/`, `src/adapters/`, `src/demo/execution/`, `scripts/demo_trader.py`, tests): multi-symbol demo execution
  generalising demo_slice (registry, per-symbol limits from MarketSpec, TP, forced-flat, demo guard, restart/reconciliation).
- Lane D (`src/demo/store.py`, `src/demo/labeling.py`, `src/demo/report.py`, `src/demo/learning/`, tests): SQLite lifecycle store +
  Parquet export, counterfactual labeller, outcome MFE/MAE/R, reports (10/25/50/100/250/500 trades), then learning (Logistic, LightGBM, River, local MLflow).

## Runner modes (scripts/demo_trader.py)
`--shadow` (zero order_send), `--demo-auto --confirm-demo-auto=I-AUTHORIZE-ACTIVTRADES-DEMO-TRADING-ONLY`, `--status`, `--analyze`.
Fail closed on: non-demo, unknown account, stale feed, reconciliation mismatch, persistence failure, unprotected exposure, broker disconnect, clock anomaly.
