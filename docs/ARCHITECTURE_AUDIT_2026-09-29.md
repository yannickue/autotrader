# Architecture Ownership Audit — Nautilus Convergence (2026-09-29)

Baseline: `ba93000` (branch `architecture/nautilus-convergence-baseline`, tag `baseline-green-2026-09-29`).
Python 3.12.14, nautilus-trader 1.231.0, MetaTrader5 5.0.6231; 677 passed / 1 skipped, ruff + compileall clean.
Sources: Haiku scout inventory, Sonnet MT5 benchmark (installed NT 1.231.0 sources; official 1.231.0 docs
NOT consulted), independent read-only Codex adversarial review (task-mumgymub-ysr3nw, 4m35s), lead spot checks.
Opus not needed: Sonnet and Codex findings agree; no unresolved critical conflict.

## Headline

`docs/ARCHITECTURE.md` declares Nautilus the primary runtime. **The code never adopted it.** `src/` has zero
`nautilus_trader` imports (only `tests/integration/test_imports.py:2-4`, a version assert); `scripts/live.py` and
`scripts/paper.py` are inert placeholders. The system is a complete custom stack (paper execution engine, portfolio,
risk engine with reservations, exit engine, SQLite state store, pipeline) with a raw MT5 wrapper that is not a
Nautilus adapter. `research/backtest.py` does not execute any of it. Convergence is therefore a real port, not a
tidy-up — but the policy layer (risk/sizing/exit/cost/margin/strategies) is largely pure and reusable.

## 3. GitHub discrepancy (understood)

`origin/sprint1/integration` = `068c3c8` = tag `sprint1-rc1`. Local is a strict descendant (`merge-base --is-ancestor`
yes), 43 commits ahead before the safety commit, i.e. nothing after sprint-1 RC was ever pushed. No divergence, no
rewrite. `origin/main` = `9139dd1` (bootstrap). Nothing was force-pushed; baseline went to a NEW branch + tag.
Left uncommitted on purpose (unfinished historical ingestion): `src/adapters/activtrades_mt5/history.py`,
`scripts/mt5_diag_inspect_rates_ticks.py` (no tests yet). Safety commit verified standalone without them (677 passed).

## 5. Current architecture map

```
data(MarketSnapshot) -> features(1 file, 31 lines) -> strategies(momentum/breakout/pullback/fusion) -> Signal
  -> pipeline.PaperTradingPipeline (owns peak equity, loss streak, exit-position mirror, fill deltas)
  -> risk.RiskEngine (sizing + gates + reservations + halt)  -> ExecutionRequest
  -> execution.PaperExecutionEngine (order book, matching, idempotency, OCO, reconcile, checkpoint)
  -> portfolio.Portfolio (positions, PnL, fees) -> exits.ExitEngine -> risk(reduce-only) -> execution
  -> persistence.SQLiteStore (positions, orders, fills, reservations, halt, checkpoints)
adapters/activtrades_mt5: MT5Connection (attach-only, lock, bounded IPC) + MT5OrderGateway (raw send) — not wired to pipeline
research/: run_backtest(callback -> TradeOutcome), replay (row iteration + hashes), Parquet store, vectorbt/, optuna/, walk_forward
```

## 6. Duplicate authoritative state owners (Codex, file:line verified by Codex)

1. Orders: `PaperExecutionEngine._orders` + checkpoint + typed SQLite `orders` (pipeline/paper.py:1442-1455 admits the duplication).
2. Positions/fills/PnL: `Portfolio` + SQLite positions/fills/portfolio rows + fill-replay rebuild (pipeline/paper.py:1636-1685).
3. Reservations/decision consumption: `RiskEngine` reservations + `PaperExecutionEngine` decision usage/cumulative fills + SQLite copy.
4. Halt/readiness: RiskEngine, ExecutionEngine mode, `RuntimeRiskState.kill_switch`, MT5 connection state (4+ machines; pipeline parses `halt_reason` strings).
5. Reconciliation: execution compares local vs snapshot; pipeline persists `last_reconciled_at=now` whenever READY (approximation); MT5 `RECONCILING` reconciles nothing.
6. Account state: pipeline rebuilds `AccountRiskState` from Portfolio; "realized PnL today" is actually all-time (no daily reset).
7. Peak equity / loss streak: pipeline-owned, persisted separately.
8. Exit position mirror `_exit_positions` (only positions opened via this pipeline).
9. Fill deltas inferred from global portfolio counters (order-of-mutation coupling).

## Responsibility matrix

| Module | Verdict | Notes |
|---|---|---|
| src/risk (policy, sizing, gates: RiskPolicy, PositionSizer) | KEEP (extract pure) | reservations, decision cache, halt state -> retire |
| src/risk (reservations/halt/state) | REPLACE_WITH_NAUTILUS | keep only narrow risk-capacity hold keyed to Nautilus order IDs |
| src/execution PaperExecutionEngine | SHADOW_ORACLE_ONLY -> REPLACE_WITH_NAUTILUS | port invariants + tests first |
| src/portfolio | SHADOW_ORACLE_ONLY -> REPLACE_WITH_NAUTILUS | fill-replay becomes verification projection only |
| src/pipeline | REPLACE_WITH_NAUTILUS (orchestration) / KEEP (TradeOutcome attribution) | strategy lifecycle -> Nautilus Strategy |
| src/persistence | DELETE_LATER (operational state); KEEP append-only audit/trade journal | |
| src/exits | KEEP as ExitPolicy; emits Nautilus order commands | drop position mirror; bootstrap from Nautilus positions |
| src/margin | SHADOW_ORACLE_ONLY (paper/backtest conservative gate) | live margin truth from venue/Nautilus |
| src/costs | KEEP (schedule, confidence gate, attribution) | map to Nautilus fee/fill models; avoid double counting |
| src/health, src/monitoring | KEEP (integrate as Nautilus actors/listeners) | |
| src/adapters/activtrades_mt5 | ADAPTER (keep connection/lock/bounded/models; wrap in Nautilus clients) | do NOT run MT5OrderGateway beside Nautilus |
| src/data, src/instruments | INTEGRATE_INTO_NAUTILUS | InstrumentSpec -> NT Instrument; records -> Bar/QuoteTick/catalog |
| src/features, signals, strategies | KEEP / INTEGRATE (Nautilus Strategy hosts them, no order/position state) | |
| src/universe | KEEP | |
| research/backtest.py, replay.py | REPLACE_WITH_NAUTILUS (currently non-simulating) | vectorbt = screening, Nautilus = promotion |
| research storage/immutability/preparation/walk_forward/optuna | KEEP | optuna stays off until baseline OOS |

Modules that duplicate Nautilus: execution, portfolio, persistence state, risk reservations/halt, pipeline state, research backtest loop.

## 11. MT5 adapter gaps vs Nautilus 1.231.0 (Sonnet, from installed sources)

Not a Nautilus adapter today (no InstrumentProvider / LiveMarketDataClient / LiveExecutionClient). Gaps:
InstrumentProvider GAP; DataClient GAP (pull-only ticks, no subscribe/QuoteTick emission); ExecutionClient GAP as
Nautilus (raw market/close/partial/SLTP/cancel-pending only; no limit/stop placement, no order events);
order/fill mapping PARTIAL (typed records exist; no OrderStatusReport/FillReport/TradeId/LiquiditySide/Money);
partial fills PARTIAL (retcode 10010 accepted but filled volume not returned); SL/TP PARTIAL (no bracket/contingent mapping);
reduce-only GAP in adapter (only in paper engine; netting close can flip); **margin_mode (netting/hedging) never read** ->
OmsType undecidable; magic PARTIAL (not set on close requests?; unverified); reconciliation/restart GAP
(no generate_order_status/fill/position/mass_status reports); duplicate-fill/trade-id GAP (no persistent deal-ticket dedupe,
no ticket<->client_order_id map); account state PARTIAL (no margin_mode, no AccountBalance/MarginBalance);
margin/commission PARTIAL; poll loop absent (MT5 has no push); reconnect PARTIAL (strong safety, no auto-reconnect / NT wiring).
**Order requests lack `type_filling`/`deviation`/`type_time` (orders.py:222-237)** — likely retcode 10030 on live send (unverified vs real terminal).
Safety invariants confirmed present: attach-only default, MT5_ALLOW_ACCOUNT_LOGIN opt-in, mismatch fail-closed,
single-owner lock (90s stale), TradingMode guard, no real MT5 in pytest (FakeMT5Client; conftest hard-block not verified).

## 12. mt5-connector (aulekator, v0.7.0, MIT) — reusable ideas

Module split (connection/providers/data/execution/parsing/factories); order-type + retcode + TIF mapping tables;
filling-mode autodetect; client_order_id in MT5 comment (persist map ourselves — comment is lossy); TradeId=str(deal.ticket);
deal-ticket dedupe set; magic filtering; commission as Money in account currency; report builders; backoff reconnect;
skipping non-trade/zero-volume deals. Use as checklist; re-derive against 1.231.0, do not vendor.

## 13. mt5-connector unsafe / incompatible

Auto `initialize()`+`login()` on every connect/reconnect (violates attach-only + explicit gate); plaintext password config;
no account-identity check; no single-owner lock; blocking MT5 IPC inside asyncio loop (no bounded worker); hard-coded
NETTING oms, no margin_mode detection; venue_position_id=None, reduce_only=False, margins=[] in reports; fixed deviation=20;
`new_tp or order.tp` cannot clear SL/TP; in-memory dedupe from UTC midnight (not restart-safe); targets nautilus>=1.190
(untested on 1.231.0); optional remote Flask/Wine container mode = different trust boundary; ambiguous licence provenance.

## 14. Convergence sequence (strangler, no big bang; never delete before parity)

- C0 (done) Baseline branch/tag.
- C1 **Invariant contracts**: restate the idempotency / overfill / reduce-only / fail-closed / stale-data / restart tests as
  engine-agnostic contract tests driven by an event stream; run them against the custom engine now (it becomes the oracle).
- C2 **Extract pure policy**: RiskPolicy+PositionSizer, ExitPolicy, cost/net-edge gates, margin gate as stateless functions of
  (snapshot, policy) -> decision. Reservations/halt stay in old engine until C6. Fix the mislabeled "realized PnL today".
- C3 **Data/instrument plane**: MT5 history -> Parquet -> NT `ParquetDataCatalog`; InstrumentSpec -> NT CFD/Index instrument
  (precision, increments, margin). Finish (currently untracked) history ingestion in this shape, GER40 only.
- C4 **Nautilus BacktestEngine**: one Strategy class (thin host over our features/strategy/policy) usable in backtest AND live;
  our sizing via a policy gate (only path to `submit_order`, enforced by a Strategy base wrapper + test); Nautilus RiskEngine
  (LiveRiskEngineConfig limits) as final admission; CFD cost/fill/latency models parametrised from demo calibration.
- C5 **MT5 Nautilus adapter (1.231.0 API)**: InstrumentProvider, poll-based DataClient, ExecutionClient wrapping our
  MT5Connection/gateway through the bounded worker (asyncio.to_thread), never exposing login; first add margin_mode,
  filling mode/deviation, persistent deal-ticket dedupe + ticket<->client_order_id map, generate_* reports with venue_position_id,
  real reduce-only enforcement. Develop entirely against FakeMT5Client (zero real MT5 in pytest).
- C6 **Shadow parity**: Nautilus (authoritative candidate) vs custom paper (oracle) on identical event streams; diff positions,
  fills, PnL, rejections. Also adapt Nautilus reconciliation into the single fail-closed exposure gate (one typed permission state).
- C7 GER40 demo live (attach-only) + restart/reconcile parity (see §17).
- C8 Retire custom execution/portfolio/reservations from the production path; SQLite reduced to append-only audit/trade journal;
  one-way migration checkpoint (no fallback after Nautilus-authority events). Dual-runtime window needs a fill-listener fan-out
  (pipeline currently overwrites `execution_engine.fill_listener` exclusively) and bootstrapping exit state from Nautilus positions.

## 15. Tests that must survive unchanged (policy/safety, engine-agnostic)

tests/unit/risk (sizing, gates, leverage, stale data/signal — reservation cases move), unit/costs (3), unit/exits, unit/margin,
unit/strategies (2), unit/data (6), unit/instruments (2), unit/health (2), all adapters/activtrades_mt5 tests
(connection, account_protection, lock, probe, config, models) + scripts tests, tests/property, research storage/immutability/
walk_forward tests. Reason: they pin business policy and the MT5 account-protection invariant.

## 16. Tests to rewrite around Nautilus (keep the *assertions*, change the driver)

unit/execution (5 files: duplicate request, duplicate trade event, same fill via two paths, fee double-charge, decision binding,
replacement-chain overfill, reduce-only clamp, late-fill protection), unit/portfolio, unit/persistence, unit/pipeline (2, incl.
recovery-inconsistency-halts and restart-never-READY), tests/chaos/test_execution_chaos.py, tests/replay (2), tests/integration
e2e paper path, risk reservation tests, unit/research backtest/replay tests (currently test a non-simulating loop).

## 17. GER40 vertical-slice plan (single instrument until every step passes)

1. MT5 history (attach-only) -> Parquet (provenance: broker tick activity, never exchange volume) -> catalog.
2. NT instrument from real `symbol_info` (digits, tick size, contract size, min/max/step, margin).
3. NT backtest with one simple strategy, our sizing policy, Nautilus RiskEngine, calibrated spread/slippage.
4. Adapter on FakeMT5Client: submit -> event -> fill report -> restart -> reconcile, plus duplicate-fill and partial-fill cases.
5. Demo: attach-only, min lot, one entry + protective stop + close; capture broker deal/position/margin/commission.
6. Restart mid-position; Nautilus mass-status reconciliation adopts the broker position; exit policy bootstraps from it.
7. Parity assertions: position, quantity, fill price, realized PnL (~), open-order state, margin — every mismatch explained.
Then NASDAQ100. WTI not a blocker. Demo results feed spread/slippage/latency/stop/partial-TP/margin calibration.

## 18. Blockers to alpha research

1. No Nautilus runtime exists in code (docs claim otherwise) — research results today cannot transfer to any runtime.
2. `research/backtest.py`/`scripts/backtest.py` score precomputed trades; replay does no trading work.
3. Historical ingestion unfinished (no GER40 dataset; history.py untested/uncommitted).
4. MT5 adapter is not a Nautilus adapter; margin_mode unknown; order requests lack filling mode.
5. No ActivTrades CFD cost model calibrated (`VenueCostSchedule` defaults are crypto-shaped; OPEN_QUESTIONS #25).
6. FeatureRegistry / OpportunityScanner / Regime Engine not built (`src/features` = 1 file, 31 lines).
7. Research trial registry existence not verified in this audit (must exist before any tuning).
8. Real-MT5 IPC from automation shells remains unreliable (accepted limitation); user-side confirmation that `uv run pytest`
   no longer logs out the terminal is still pending.
9. NT 1.231.0 official docs were not consulted (only installed sources) — verify adapter contracts against the tag before C5.

## Unverified / open

Whether ActivTrades demo/live is netting or hedging; filling-mode requirements; commission model; whether close/partial-close
requests set magic; conftest hard-block on real MetaTrader5 import; Haiku inventory claim that research/ is "notebooks" was wrong
(it contains a callback backtest, vectorbt/ and optuna/ dirs) and was discarded.
