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

## Lane R2: catch-up, non-traded counterfactuals, accounting, idle markets (2026-09-30)
- **Closed-bar catch-up.** `bar_pointers(market, timeframe=M5)` in the DemoStore holds the CLOSE of the newest fully processed bar
  (monotonic upsert, survives restarts; no pointer yet = only the newest bar is evaluated, history is never replayed). Every cycle
  evaluates ALL closed bars after the pointer, oldest first, through `OpportunityEngine.on_m5_close(market, bar_close, catchup=CatchupInfo(live_now, live_quote))`:
  data truncated at that bar, synthetic quote = bar close + recorded bar spread, snapshot `signal.origin = CATCHUP`. A past bar
  (age > one M5 bar, `live_max_age_s`) is NEVER traded: an opportunity the policy would accept is recorded as rejected with
  `CATCHUP_MISSED, EXPIRED_ENTRY[, ALREADY_MOVED]` (ALREADY_MOVED = the live price left the entry tolerance); an engine reject keeps its own
  gate codes. Bounds: 300 bars / 24 h per market and cycle (`skipped_old` counted); bars inside a calendar-closed period are skipped
  (`skipped_closed`). The pointer advances only after the bar was processed; an engine exception is retried once, then stored in
  `scan_errors` (auditable) and the bar is skipped. Bars are scanned and recorded also while halted (`_execute` then cancels with `halted`).
  The seen id is committed in the same transaction as the snapshot (`StoreSeenAdapter` keeps it in memory until then).
- **Counterfactuals for every non-traded opportunity**: engine rejects, catch-up misses, stack rejects (size_below_min, margin, ADDON_*, spread,
  ...), cancelled (halted / expired / restart), send-failed, shadow dry-run (`counterfactual_meta.source` keeps it distinct) and accepted
  decisions that never got an intent. Causal bars only, fill assumed AT the intended entry with no costs (optimistic), `counterfactual_meta`
  stores source + blocking gate code/class. The funnel reports per gate: count, share, per market/family/session/hour, n labelled, mean MFE_R /
  MAE_R / R, target-before-stop share; stages RAW..EXECUTED; MISSED / EXPIRED / ALREADY_MOVED / ADDON / OPPOSITE buckets; opportunities per
  market per local hour; family/market/cluster shares and the near-duplicate ratio (same market+family+direction+0.25 ATR stop zone+day).
  `OUTSIDE_ENTRY_WINDOW` stays "not observable" (family entry masks never emit out-of-window signals).
- **Accounting.** `trade_tags`: STRATEGY (default, also for legacy rows) | EXECUTION_CANARY | TEST_TRADE, plus `censored`. Exits other than
  STOP/TARGET/SESSION_END (MANUAL, EXTERNAL, SAFETY_FLATTEN, emergency flatten, unknown hints) are censored. `DemoStore.list_outcomes/count_trades`
  default to `kind="strategy"` (uncensored STRATEGY trades): strategy expectancy, winrate, cumulative R, milestones and learning labels use only
  those; censored and canary trades are reported separately (n, R, MFE/MAE, duration) plus an account P/L reconciliation. `python scripts/demo_trader.py
  --record-canary FILE.json` (`demo.external.import_external_trade`) records a trade executed outside the runner from deal-history fields; it never
  touches the broker. `account_id_hash` and `account_phase` (`--account-phase`, ALPHA_EXECUTION_DISCOVERY | SMALL_ACCOUNT_FEASIBILITY | custom) are
  stored once in the store meta and in `<artifacts>/account_meta.json`; attaching a different account to that store/artifacts dir fails closed at
  start (an old DB without the meta is recorded and flagged legacy). Every report/heartbeat states `DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF`.
- **Closed markets.** A stale feed is idle only if the MarketSpec calendar says closed AND there is no fresh broker quote; a market that should be
  open and is silent is `STALE_FAULT` (all-stale halt/exit). While every enabled market is idle the poll interval is `idle_poll_interval_s` (30 s,
  heartbeat stays fresh), account snapshots run every `idle_account_check_s` (60 s) and a broker disconnect without exposure is tolerated for up to
  `idle_transient_grace_s`. On resume the bars since the pointer are caught up (closed hours skipped). Heartbeat: `market_state`, `idle_all_markets_closed`, `catchup`.
- **TCA / timing.** `tca_records(ENTRY)` carry `decision_price`, `order_arrival_price`, `requested_price` (None: the stack events do not provide it),
  `actual_fill_price`, `decision_to_arrival_drift`, `implementation_shortfall` (+ `_r`), `movement_to_cost`; `outcome_extra` carries `signal_age_at_fill_s`,
  `time_to_0.25R/0.5R/1R_s`, `time_without_progress_s`, `mfe_giveback_r` (bar resolution).

## Lane E (stage E1): ExitEngine wired into the DEMO stack

Scope: the existing deterministic `exits.ExitEngine` (no second engine) now manages open DEMO positions when
`StackConfig.exit_policy == "staged"`. The broker-side stop stays the safety backstop throughout. Chart-structure target
derivation (which produces the `target_price` stages) is NOT part of E1 (lane E2); E1 accepts stages via the contract.

- **Activation.** `StackConfig.exit_policy`: `fixed_1_5r` (DEFAULT, today's behaviour, byte-identical: broker SL + the intent's one
  fixed-R broker TP + forced flat at flat_min; `manage_exits` returns `[]` and does nothing) or `staged` (requires
  `StackConfig.staged_exit`, an `ExitPolicy`). `staged` is NOT the default.
- **Runner hook.** `DemoRunner._manage`: `poll_events()` -> `stack.manage_exits(now)` (optional port method, skipped for stacks
  without it) -> `on_clock(now)`. Deterministic, no LLM, repeated every cycle.
- **Contract.** `TakeProfitStage(close_fraction, r_multiple | target_price (exactly one), stage_id, source)`; `source` is `"R"` or
  `"STRUCTURE:<id>"`. `close_fraction` is a share of the ORIGINAL quantity and family-configurable (never hardcoded thirds).
  `ExitPosition.target_stages` carries a per-position ladder that replaces the policy ladder and is validated: strictly
  ordered in the favourable direction beyond entry (LONG `stop < entry < TP1 < TP2`, SHORT mirrored), fractions sum <= 1.
  A stage list may be TP1 + runner; a second target is never invented (`SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED`). R is
  computed from chart prices afterwards; the legacy `r_multiple` ladder is unchanged (baseline/shadow comparison).
  After entry a stop may only stay or tighten (`ExitPosition` rejects a current stop looser than the initial one; the engine
  only ratchets forward; `ModifyStopJob` and the adapter re-check).
- **Plan input.** `submit(intent, context)` reads `context["exit_plan"] = {"stages": [...]}` (persisted in the registry row
  context with the original quantity, atr, family). No plan => the policy ladder, or nothing (broker SL only).
- **Broker TP under `staged`: ABSENT, or the FINAL stage only.** The intent's fixed-R target is NOT sent. A broker TP is placed only
  when the final stage is an absolute price beyond entry and the fractions sum to 1 (no runner). An R-based final stage has no
  known price before the fill, so the engine closes it at market; with a runner there is no TP to cap it.
- **Jobs.** `ReduceJob` (reduce-only MARKET of a given quantity, tagged with the decision id) and `ModifyStopJob` (tighten only)
  in `DemoTraderStrategy`; full closes reuse `FlattenJob`.
- **Admission (thin, reduce-only).** Fresh broker read: exactly one own-magic position, side = registry direction, ticket match,
  `quantity < broker volume`, lot step and broker min lot (below min => skipped and counted, never sent), quote present and
  younger than `min(max_quote_age_s, policy.max_market_data_age)` (a missing/stale quote means NO decision; the broker stop remains,
  the engine's "stale data => emergency close" fail-safe is deliberately not triggered from a feed hiccup). The adapter then
  re-applies its own reduce-only checks (own magic, opposite side, quantity <= position, local == broker signed quantity).
- **Adapter invariant.** After a partial `KIND_EXIT` fill the Nautilus SL/TP child orders are resized to the remaining broker
  volume (`OrderUpdated`, never enlarged; failures are retried from broker truth on the next sync), so
  `broker_open_quantity == local_remaining_quantity == stop-protected quantity`. MT5 has ONE position-wide SL/TP (unchanged by a
  partial close), so there is no unprotected window. `PROTECTIVE_QUANTITY_MUST_EQUAL_POSITION` for NEW protective submissions is
  unchanged. A stop move is one atomic SLTP request (no cancel/replace window); if it is rejected the previous stop stays in force
  and is retried next cycle; a missing local stop child (restart adoption) falls back to the broker-verified `emergency_protect`
  (tighten-only); if the broker stop disappeared during a modification the existing protection repair (restore or flatten) runs.
- **State persistence.** `stages_completed`, high-water mark, stop stage and the partial-exit records live in the registry row
  context (`exit_state`), written only AFTER the reduce is verified at the broker. `stages_completed` is also lower-bounded by the
  realized volume (`original - broker volume`), so a crash between fill and persist cannot re-fire a stage. An under-filled stage
  counts as done (no chasing; the remainder runs with the runner).
- **Tranche record per partial** (`stack.exit_log()`): `intent_id`, `tranche_id`, `strategy_family`, `quantity_before`,
  `quantity_reduced`, `quantity_remaining`, `realized_r_of_position`, `remaining_risk_r`, stage id/source.
- **Risk reservation.** `DemoRiskGate` only sizes ENTRIES and keeps no reduce-only reservation; `ExitEngine.notify_terminal` is
  called on every terminal outcome with a recording no-op gate (N/A for the DEMO risk book).
- **Known limitations (explicit, not improvised).** MT5 nets per symbol: with more than one live tranche on a market the engine is
  NOT run for it (`EXIT_PLAN_MULTI_TRANCHE_NOT_SUPPORTED`, TEMPORARY; add-ons stay rejected). Engine-driven FULL closes carry
  `exit_reason` `MANUAL` (the recorder's exit-reason set is owned outside this lane); the engine reason is in the registry row
  `detail` (`exit_engine:<REASON>`) and `exit_log()`.

## Clock reference (2026-09-30)
A broker tick timestamp is a market-event time, not a continuously advancing wall clock. `Mt5DemoStack._lane_snapshot` therefore reports the
FRESHEST tick over all configured markets (never the first market) as `server_time`. The runner (`_check_clock_reference`) uses it as a clock
reference only while it keeps ADVANCING (within `clock_reference_window_s`, a criterion independent of the local clock); then `abs(skew) > max_clock_skew_s`
=> `clock_anomaly` fail-closed in both directions. No live reference (paused market, weekend) => heartbeat `clock_reference = UNAVAILABLE`, quote age is
NOT read as clock skew, and the existing stale-feed / session / closed-market logic alone governs exposure. A tick AHEAD of the local clock by more than
`max_clock_skew_s` is fatal with or without a live reference (a stale quote cannot be in the future). Trigger: the runner stopped at 20:04 UTC with a
false 305 s skew because the first market was paused.
Architecture note: `src/coverage_analysis` (offline hindsight analysis) and `markets/phase2.py` (offline preflight cost wiring, only
`alpha.common.market_costs`) are the only non-alpha, non-`demo.opportunity` code allowed to import `alpha`; dedicated tests keep them off execution/risk.

## Lane M2: Brent + BTCUSD in the DEMO trader (2026-09-30)

Both markets are wired end to end and OFF by default (`configs/markets_phase2/enablement.toml`: `BRENT`/`BTCUSD` `enabled = false`).

Enable / disable (per market, DEMO only; BTCUSD first, Brent after a GREEN at market open):
1. Edit `configs/markets_phase2/enablement.toml`: `[BTCUSD] enabled = true` (later `[BRENT] enabled = true`). No other key, no code change.
2. Restart the runner (`scripts/demo_trader.py --shadow` first, then `--demo-auto ...`). Without `--markets` the universe is the five
   markets + every enabled Phase-2 market; `--markets BRENT` on a market that is not enabled is refused (exit 2).
3. Disable = set the flag back to `false` and restart. A market whose intent-registry rows are still open (an open position) is NOT
   dropped: it is kept registered in MANAGE-ONLY mode (`start_notes.manage_only_markets`, `disabled_markets` reason `manage_only: ...`):
   entries reject with `market_disabled`; exits, forced-flat, protection repair and reconciliation stay active; the position is not
   foreign and the core markets keep trading. If such a market cannot be verified at start (symbol gone / facts mismatch) the stack
   REFUSES TO START (`open_exposure_on_unverifiable_market:<market>`): flatten by hand first. A market with no open exposure is simply
   not registered. (Limit: a broker position with no registry row on an unregistered Phase-2 symbol is still classified foreign and halts.)

BOOTSTRAP / SAFETY SCHEDULE — not alpha-validated; must not become an undocumented permanent rule: the BTCUSD Mon-Fri window, entry
cutoff ~19:30 UTC and forced flat 20:30 UTC are a provisional safety schedule, not a validated edge window.

Margin note (small accounts): an open BTCUSD min lot needs about 370 EUR margin (observed 369.69 EUR at 83746.28, 2x). That margin is no
longer free, so a core-market entry can fail on the `max_margin_fraction_of_free_margin = 0.90` cap (`sizing.py`) on a small account.
The start preflight reports it as information only (no gate): `start_notes.phase2_preflight.<M>.free_margin_eur` and
`min_lot_margin_pct_of_free_margin`.

Double gate: (1) the flag, (2) a per-market START-UP PREFLIGHT inside `Mt5DemoStack` (`markets.preflight.run_live_preflight`, live broker
facts): exact broker symbol/path, `trade_mode` FULL, valid + fresh quote (<= 30 s) WHILE the market's calendar says open, contract/volume
facts vs the checked-in config, `order_calc_margin` implied leverage (<= 30x and >= the spec leverage), `stops_level` headroom for the
reference structural stop, SL + filling allowed, repo wiring. A failing market is DISABLED ALONE: the reason is in the heartbeat
`disabled_markets` (runner prefix `stack_preflight:`) and in `AccountSnapshot.extra` (`disabled_markets`, `start_notes.phase2_preflight`);
new entries on it reject with gate `market_disabled`; the runner never scans it; the stack and the five core markets keep running. The
runner only fails closed if EVERY market is disabled. A market in its break (calendar closed + stale quote) is IDLE, not a fault (R2). A
symbol that does not exist / is not tradable / has a foreign path is dropped at instrument-load time (`instrument_load_failed`, intents
-> `unknown_market`). A disabled market is re-checked only at the next restart.

Strategies: frozen production spec v1 (hash `c3eae99e782888ac`, five markets, untouched, still the default) and v1.1 (hash
`4f4b33e97966cd84`, `production_spec_v1_1.json`, a STRICT superset: the five v1 markets verbatim + BRENT/BTCUSD). v1.1 is loaded only when
a Phase-2 market is enabled. Enabling therefore changes the `strategy_hash` stamped in every snapshot (new hash, documented here); with
both flags off nothing changes (golden fingerprint + `test_default_runner_is_the_five_markets_on_the_frozen_v1_spec`).
Family set for BRENT/BTCUSD: ORB breakout + ORB fade (class-default parameters, no thresholds). GAP / OVERNIGHT / VOLREV / EOD need
Train-fitted quantiles and no Phase-2 history exists at or before the fit end (probe bars start 2026-07-28 / 2026-08-17 for M5), ROUND needs
an unresearched round-number scale (`ROUND_TICKS` has PROVISIONAL entries for `energy_cfd`/`crypto_cfd` only so `_assemble` can build the
context features), LEADLAG has no leader pair for energy/crypto: these are documented GAPS (research work: download history, fit, seal v1.2).
No parameter was tuned on these markets.

Calendars (PROVISIONAL, Mon-Fri only, out-of-window = shadow only): BRENT London 08:00-18:00, entries 08:00-16:30, flat 17:55 (inside the
observed bars: server 02:00-22:55 daily, break 22:55-02:00). BTCUSD UTC 08:00-20:30, entries 08:00-19:30, forced flat 20:30 (moved from
21:55 so it stays before the observed Friday 22:55 server = 20:55 UTC break); no automatic 24/7, no weekend entries.

Margin / leverage: unchanged generic logic. The sizer takes `margin_per_lot` from `order_calc_margin` at the executable price (F2
reference price) and caps the position at `min(spec.max_leverage, account leverage, 30)`; the observed instrument leverages are BRENT
9.99x (min lot 86.32 EUR at 97.78) and BTCUSD 2.00x (min lot 369.69 EUR at 83746.28), i.e. `max_leverage` 10 / 2. The broker's
account leverage 1:30 is NOT the instrument leverage and `price x contract / 30` does not reproduce the observed margins. Structural
stops are used as given; no stop is tightened; min-lot vs hard-cap logic is unchanged.

## Lane P: live operating policy, flatten deadline, broker sessions, `--daily` (2026-10-01)

User rules: trades after 21:00 Europe/Berlin are allowed; at 22:00 Europe/Berlin zero strategy-managed (own-magic) positions; all
times Berlin (DST-aware via zoneinfo); only the final flatten window (plus a 10 min entry runway) blocks new exposure.

* `configs/live_operating_policy.toml` (+ `demo.opportunity.operating_policy`): LIVE-only, versioned (`version` + `policy_hash` are
  written to every snapshot as `versions.operating_policy`). Research SimWindows / frozen family windows / `market_spec_hash` are NOT
  touched: the policy is an overlay (`clock.live_spec`) used by the live engine only. `flatten_start` 21:55, `global_flat_deadline`
  22:00, `broker_close_buffer_min` 5, `min_entry_runway_min` 10 (two M5 bars: a later entry would be force-closed before it can be
  managed and would race the flatten phase). Per-market `entry_end_live` (NAS100/SPX500 = 15:55 New York, clamped to the local flat)
  lets those markets enter after 21:00 Berlin up to the runway; EURUSD / XAUUSD / GER40 keep their strategy windows.
  Side effect to know: the EOD family computes its session end as `min(cash_close, entry_end)`, so the widened NAS100/SPX500
  `entry_end` also moves EOD's window there (frozen family code untouched; decide before trusting those two markets' EOD results).
* `clock.forced_flat_utc(spec, entry_utc, exit_min, operating=None)` stays the single choke point: earliest of the market-local flat,
  the Berlin flatten start and (broker session close - buffer). `operating=None` is bit-identical to before. The runner start-up
  self-check now verifies the MARKET-LOCAL flat (`market_flat_utc`) and, with a policy, that the effective flat never exceeds the
  global deadline (BTCUSD summer 20:30 UTC = 22:30 Berlin is moved to 21:55).
* `Mt5DemoStack.on_clock` sweep (`StackConfig.operating_policy`, default None = unchanged): from the flatten start (earlier for an
  instrument whose broker session closes earlier) EVERY own-magic broker position is closed reduce-only, also rows that are not OPEN
  in the registry and after a restart (broker truth is re-read before and after every attempt; a failed/uncertain close never flips
  exposure; bounded backoff 5/10/20/30/60 s; never given up). Heartbeat: `flatten_state` IDLE | WINDOW | OVERDUE | FLAT_CONFIRMED,
  `eod_flat_confirmed_utc`, `eod_detail`. Not flat at 22:00 = OVERDUE + halt + `eod_flat_overdue` error every 60 s, retries continue.
  Entries are refused from the flatten start (`flatten_window_active`, stack gate + runner gate) and inside the runway
  (`entry_runway_too_short`).
* Foreign-position gap: an own-magic position whose symbol the stack cannot map (e.g. a Phase-2 symbol without registry row) stays
  fail-closed (`foreign_position_at_broker` halt in `poll_events`, unchanged) and is NOT swept (no registered instrument to close it
  through; no second exit path was added): the sweep reports it as `unmapped_own_position:<symbol>` (state WINDOW/OVERDUE) and halts
  new exposure. Phase-2 markets with open registry rows are adopted in manage-only mode at start and ARE swept.
* Feed semantics (runner, only with a policy): `should be open` = broker session (known daily pauses, weekend) instead of the cash
  session. A known broker pause is `KNOWN_SESSION_PAUSE` (idle, never counts towards all-stale, healthy markets keep trading,
  on_clock keeps managing positions); silence inside an open broker session (also after the cash close) is `STALE_FAULT`. Session
  schedules: NAS100/SPX500/XAUUSD daily break 23:00-00:00 Berlin, EURUSD Fri 23:00 close, GER40 22:00 Berlin -> 00:15 UTC
  (all `observed` from the M5 history, summer and winter); BRENT 20:55-00:00 UTC and BTCUSD Fri 20:55 -> Sat 07:00 UTC are
  `provisional` (single summer probe). US/EU DST-mismatch weeks are not observed for the broker breaks.
* `scripts/demo_trader.py --daily` (default off): after 22:00 Berlin, once the stack reports FLAT_CONFIRMED, the account is
  RECONCILED with 0 positions and no open/in-doubt intent is left, the runner finalizes (final label/report) and exits 0 with
  `stop_reason = eod_flat_shutdown`; otherwise it keeps running. Entries outside Mon-Fri (Berlin date) are refused
  (`outside_operating_day`). The CLI always loads the policy; `build_live_runner(operating_policy=None)` stays the library default.

### Lane Z - zero-overnight safety fixes (Opus audit C1, H1, M1, M2, M3, M5, BTC window)

* C1 `Mt5DemoStack`: with an operating policy the row-based forced flat uses `escalate=False`; the end-of-day sweep counts failures
  only in `_eod_failures` (never in the shared `_flatten_failures`), so the fatal `flatten_failed:<M>` can no longer pre-empt the
  retrying sweep. `_flatten` judges success by OWN-magic positions only (a foreign/canary ticket on the symbol neither fails nor is
  closed; the adapter's reduce-only path also filters to the own magic). `on_clock` still runs (row loop + sweep) while a fatal other
  than `non_demo_account` / `account_identity_changed` / `mt5_lane_timeout` is latched, and the runner's `_manage` calls `on_clock`
  even when `poll_events`/`manage_exits` raise (the failure is re-raised afterwards): flatten attempts only reduce exposure.
* M1 a position the Nautilus cache never adopted (strategy answers `flat/no_open_position`) is closed by broker ticket through
  `exec_client.emergency_close` (reuses the reduce-only checks: own magic, opposite side, never larger than the position, ticket
  re-read at the broker; booked like a restart-time fill).
* H1 supervisor: while the last heartbeat of today shows own exposure (open positions / intents / flatten WINDOW|OVERDUE) the 22:15
  cut-off and the restart/give-up budgets are suspended, bounded by 23:30 Berlin; exit 0 is "done" only with
  `stop_reason == eod_flat_shutdown`; STOP file + exposure in the flatten window writes a CRITICAL `watchdog_alert.json`. Runner:
  `_has_open_exposure` counts open intents also while disconnected (no more immediate exit 7), a STOP inside the flatten window
  finishes the sweep first (`stop_flatten_grace_s` = 900 s after the deadline) and a shutdown with exposure left logs
  `overnight_exposure_at_shutdown`. Task XML: the 08:30 trigger repeats every 15 min for 15 h (until 23:30) and a logon trigger
  (1 min delay) re-enters the supervisor after reboot/sleep (no BootTrigger: needs admin). `IgnoreNew` + the two locks keep it single.
* M2 `--daily` is passed explicitly (`run_trader_day.ps1 -Daily on`, supervisor default `on`); `--exit-policy` is untouched.
* M3 the EOD family keeps its frozen window: `MarketCalendar.research_entry_end_min` / `semantic_entry_end_min` (set only by
  `engine.live_family_calendar`) pins `session_end` and `effective_window`; the live `entry_end_live` extension gates entries only.
* M5 start failures (never READY, exit 3/7/8: MT5 not running / not reachable) retry every 60 s for 45 min, then every 5 min until
  12:00 Berlin with a loud alert, outside the crash budget (never starts MT5); crashes after READY keep the old budget.
* BTCUSD: `entry_end_live = "flat"` (policy `live-op-2`): entry end = effective flat - runway, computed per day (summer 19:45 UTC =
  21:45 Berlin, winter 20:20 UTC). The research forced flat (20:30 UTC) is deliberately NOT extended, so the winter window ends
  earlier than the 21:55-Berlin / Friday-break bound. BRENT is unchanged until the winter break is observed.
* L1-L5 were not enumerated in the Lane Z brief and are therefore not documented here.
