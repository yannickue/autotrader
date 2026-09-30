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
entry only inside SimWindow, forced flat at `flat_min` (reduce-only close). There is NO one-position rule in the opportunity policy or the runner: broker netting (one
net position per symbol) is only the broker representation; same-symbol add-on / opposite-side handling belongs to the stack (temporary `ADDON_*` codes).

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


## Lane I: runner <-> stack wiring (2026-09-30)
- `build_live_runner(mode)`: `shadow` -> `Mt5DemoStack(dry_run=True)`, `demo-auto` -> `dry_run=False`; the real client and attach-only config are
  obtained lazily inside the factory only; `MT5_ALLOW_ACCOUNT_LOGIN=1` is refused; stack state under `artifacts/demo_trader/stack`.
  Learning defaults ON in shadow, OFF in demo-auto (`--learning` opts in); the trainer runs in its own thread on its own DB connection, only while flat.
- ONE bar-source interface for engine, runner and stack: `m5_frame / latest_quote / last_closed_bar_close_utc` (`LiveBarSource`).
- `submit(intent, context=...)` carries family (concentration cap), atr and LOGGED-ONLY quality inputs (confidence, confluence, quality components,
  independent clusters, win probability / expected payoff from shadow predictions, `None` when absent). They never size or gate a trade.
- Persisted per intent: `risk_detail` (ACCEPTED and REJECTED, exact reject code + gate class), `tca_records` (ENTRY at the fill, EXIT at the close).
  Cost semantics (signed broker amounts, negative = cost): `Fill.commission/swap` = entry deal, `PositionClosed.commission/swap` = closing deal(s);
  total = entry + closing, computed once from the immutable first execution record; `cost_status=verified` only if the broker's closing deals supplied both.
- Rejection funnel (`demo.funnel.funnel(store, stack)`): engine reasons by class (`GATE_CLASSIFICATION`), stack rejections by class
  (`GATE_CATALOG`), TEMPORARY `otherwise_valid_blocked`, per market / family, "trades that would have existed"; in the heartbeat
  (`rejection_funnel`), `--analyze` (stderr + `funnel-<phase>.json`) and every report.
- Resilience: `IN_DOUBT` (order outcome unknown) is non-terminal; transient conditions (reconciliation != RECONCILED, disconnect, stack halt, all
  expected-open feeds stale) halt new exposure without latching the stack, keep managing, back off, resume by themselves and exit 7 only after
  `transient_grace_s` (stale feeds: `all_stale_exit_s`). Closed markets (MarketSpec calendar) are idle, not stale.
  `--forced-flat-on-shutdown` (default off) only acts if the stack offers `flatten_all`; `StackPort` has none, so positions stay broker-protected.

## Shadow dry-run pipeline (Lane S)
- `--shadow` now drives every engine-accepted opportunity through the STACK's risk/sizing/gate pipeline (`Mt5DemoStack(dry_run=True)`,
  `order_send` hard-guarded by `ShadowGuardClient`, `order_check` only). The runner records the intent (PLANNED) and calls `stack.submit`
  exactly like demo-auto. `Accepted` -> `RISK_APPROVED` -> `CANCELLED{"reason":"shadow_dry_run"}` (never SENT/FILLED); `Rejected` ->
  `RISK_REJECTED` with the same reject code, gate class and `risk_detail` persistence as demo-auto. `risk_detail` (ACCEPTED) is persisted too.
- Funnel/heartbeat: shadow-approved intents are counted as `shadow_would_trade` (also per market and in "trades that would have existed"),
  NEVER as `traded`; no execution/outcome rows exist for them, so reports and learning records (closed trades) exclude them. Counterfactual
  labelling stays for engine-REJECTED opportunities only.
- Guards: a shadow runner refuses (fail closed) a stack that is not `shadow`; any Fill/ProtectionConfirmed/PositionClosed event in shadow fails closed.
- Limitation: the dry-run stack never holds a position, so shadow decisions are stack-independent across same-symbol repeats. `ADDON_*`
  (add-on / opposite-side) rejects can NOT be observed in shadow; only demo-auto exercises them.
