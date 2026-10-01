# E2 execution-contract canary (ActivTrades DEMO)

`scripts/e2_broker_canary.py` proves, on the real ActivTrades DEMO terminal, the broker behaviour the `staged` exit
policy depends on: **partial reduce-only close** and **tighten-only stop modification**, with protection staying correct
after each step and a clean restart/reconciliation at the end.  It is an *execution-contract* test only: no strategy, no
alpha conclusion.  **DEMO only.**

## What it does

It reuses the production machinery and adds no execution path: `Mt5DemoStack` (same attach-only config as the trader),
the normal entry path with a staged `exit_plan`, `ReduceJob` / `ModifyStopJob` / `_flatten` -> `emergency_close` (the
calls `StagedExitManager` makes), the Nautilus strategy and the MT5 adapter.  The canary stack differs from the trader
stack in exactly two settings: `magic = 740099` (the trader's `canary_magic`, so the trader's accounting censors these
trades and reconciliation can tell them from strategy positions) and `exit_policy = staged`.  The position is tagged
`EXECUTION_CANARY`; a `canary_trade_<utc>.json` record compatible with `demo_trader.py --record-canary` is written.

Market `EURUSD` (default, `--market`), LONG, size exactly **2 x broker min lot** (0.02).  The risk fraction is
calibrated through the real sizer on a shadow stack (`order_send` hard-guarded, real `order_check`) until it yields
exactly 2 x min lot; the run aborts (REFUSED) if it cannot.  Initial stop distance =
`round_up_tick(max(10 x spread, 3 x stops_level points, 3 x freeze_level points, 0.15 % of price))` (about 17 pips on
EURUSD), so it cannot trigger during the ~1 minute run.  The exit plan is TP1 (price stage, close_fraction 0.5, at 3 x the
stop distance, never reached) + runner, so no broker TP exists (`broker_target_for_staged`).

| # | Step | Broker/local truth asserted |
|---|------|-----------------------------|
| 1 | open 2 x min lot via the normal entry path | events Accepted/Fill/ProtectionConfirmed; broker volume = 0.02, magic 740099, BUY, no TP; registry OPEN |
| 2 | broker protective stop | sl > 0, below entry, within 1 tick of the requested stop, covers the full volume (stop child qty == position) |
| 3 | partial reduce-only close (`ReduceJob`, 1 x min lot) | outcome `reduced`, filled == requested |
| 4 | volumes | broker remaining == Nautilus remaining == expected |
| 5 | protection after partial | broker SL unchanged and present; adapter stop child qty == remaining == broker volume (`PROTECTIVE_QUANTITY_MUST_EQUAL_POSITION`); no orphan canary order |
| 6 | stop tightens (`ModifyStopJob`) | broker sl == requested, strictly closer to price, valid vs stops_level/freeze, volume unchanged |
| 7 | stop cannot loosen | engine guard `stop_is_unchanged_or_tighter` refuses; `ModifyStopJob(wider)` is `denied/stop_not_tighter`; broker sl unchanged |
| 8 | second partial | only if the remainder allows it (with 2 x min lot: `NOT_APPLICABLE`, the second stage is the final close) |
| 9 | final reduce-only close | no canary position, no opposite position, no order on the symbol, registry `CLOSED` |
| 10 | restart | stack stopped, NEW stack on the same state dir: `RECONCILED`, flat, no orders, no open intents, registry row terminal, no foreign positions |

Every step records broker snapshots before/after (positions + orders; no secrets), and the run records order / partial /
modify / final-close latency, entry slippage vs the quote (adverse positive), spread and commission.

## How to run

```
# static plan, no broker at all
uv run python scripts/e2_broker_canary.py --dry-run-plan [--plan-price 1.17]

# read-only against the real terminal: every refusal check + calibrated sizes, places nothing
uv run python scripts/e2_broker_canary.py --live --dry-run-plan

# the canary (trader runner MUST be stopped; ~1-2 minutes; hard timeout 120 s for the order sequence)
uv run python scripts/e2_broker_canary.py --live --confirm-demo-canary=I-AUTHORIZE-ACTIVTRADES-DEMO-CANARY-ONLY

# tests / CI: the same code against the netting-account fake broker
uv run python scripts/e2_broker_canary.py --fake
```

Output: console log (PASS/FAIL per step) and `artifacts/e2_canary/canary_report_<utc>.json` (live is pinned to this
directory).  Exit codes: `0` EXECUTION_CONTRACT_PASS, `1` FAIL(step n, reason), `2` REFUSED (nothing sent), `3` internal
error, `4` canary exposure may remain at the broker (**manual action required**).

## Safety properties

* Attach-only; `MT5_ALLOW_ACCOUNT_LOGIN=1` is refused.  DEMO only (`trade_mode == 0`), expected login/server, and the
  attached `account_id_hash` must equal the one the trader is bound to (its store meta, else its last heartbeat, else
  `--expected-account-hash`; unknown binding = refusal).
* Live needs the exact `--confirm-demo-canary` phrase and the artifacts dir `artifacts/e2_canary`.
* Refused while a runner/supervisor lock is alive, a fresh trader heartbeat exists, or the MT5 terminal lock is held.
* Refused unless the quote is fresh and the broker's `order_check` accepts the canary entry (market open), the symbol is
  tradable, and the account has **no position or order at all** (any magic; the symbol in particular), because the stack
  halts new exposure on foreign positions anyway.
* Only positions with magic 740099 are ever touched (`_flatten` / `emergency_close` verify magic, side, volume).
* try/finally + SIGINT/SIGTERM/SIGBREAK + a hard overall timeout: on any failure, exception, signal or timeout the canary
  position is flattened reduce-only (`stack._flatten`, fallback `emergency_close` by ticket, last resort a recovery
  stack) and the residual broker state is reported; the verdict is `FAIL(exposure ...)` / exit 4 if anything remains.
* No retries of exposure-changing requests beyond the bounded flatten attempts; the stack never retries sends.

## What PASS enables (scope decision stays with the lead)

`EXECUTION_CONTRACT_PASS` on the real DEMO means partial reduce-only close, tighten-only stop modification, post-partial
protection resizing and restart reconciliation are proven against ActivTrades.  The `staged` policy and the structure
geometry become **technically eligible** for DEMO activation (`demo_trader.py --exit-policy staged`).  It says nothing
about edge; whether to activate, for which markets, and with which geometry is a separate decision.

## What FAIL means

Keep `fixed_1_5r` (the default) and keep the E2 shadow comparisons.  Record the exact blocker from the report
(`verdict`, the failing step's `detail`, the before/after broker snapshots) in the ledger; do not enable `staged` until a
re-run passes.  `FAIL` at step 6 with "no valid tighter stop" means the market moved away: re-run, it is not a contract
verdict.  `exit 2` (REFUSED) is not a failure of the broker contract: nothing was sent.

## Known differences fake vs real broker

* The fake broker is a **netting** account (as ActivTrades): one net position per symbol, SL/TP per position.  A hedging
  account is out of scope (the stack refuses it).
* Fills are instant and deals are visible immediately in the fake; on the real terminal deal visibility can lag, so
  steps 4/5/9 poll (bounded) before asserting.  Latency numbers are only meaningful live.
* The fake accepts IOC; the real broker's allowed filling modes are handled by the adapter (not exercised by the fake).
* The fake has no freeze level, requotes, spread widening or slippage unless a test injects them.

## Finding while building this (for the lead)

`ExecClient.emergency_protect` / `apply_protection` is "tighten-only" by documentation, but the adapter itself only
*gates* a loosening on READY + RECONCILED (`PROTECT_LOOSEN_REMOVE`); it does not refuse it.  The tighten-only invariant of
the staged path is enforced one layer up (`stop_is_unchanged_or_tighter` in the exit manager and the strategy's
`stop_not_tighter` denial).  Step 7 therefore verifies those production guards and deliberately does **not** attempt an
adapter-level loosening on the real account.

## Lane K: scenarios through the REAL `manage_exits` path (`--scenario`)

Why: the 10-step canary above drives `ReduceJob` / `ModifyStopJob` DIRECTLY (`exit_policy="staged"`).  That proves the broker
contract but bypasses `StagedExitManager` and the profile routing.  Before `staged_profiles` may be activated, the same broker
behaviour must also be shown through the production decision path.  `--scenario` selects what runs; **the default stays `base`
(the 10-step canary above, unchanged behaviour)**.  `--scenario all` runs `base, b1, b2, b3` sequentially, each with its own
stack, finally-flatten and report, with a read-only flat-account check (any magic, orders included) after every scenario; it
stops at the first non-PASS or non-flat account (exit 4 if canary exposure remains, 2 if something foreign appeared).

The b-scenarios use `exit_policy="staged_profiles"` and a profile row produced by the real Lane Y producer
(`produce_exit_context` with the route of the scenario's family/mode: STRUCT/breakout = CONTINUATION, ROUND/reject = REVERSION),
persisted in the registry exactly like the runner does.  Size 2 x min lot, magic 740099, tag `EXECUTION_CANARY`, same refusals.

| Scenario | Steps (names in the report) | What it proves |
|----------|------------------------------|----------------|
| `b1` CONTINUATION | `entry_profile_row`, `broker_protective_stop`, `engine_tp1_partial`, `volume_broker_eq_local`, `protection_after_partial`, `engine_structure_trail`, `engine_cannot_loosen`, `engine_final_close`, `restart_reconcile` | the ENGINE (not the canary) decides the TP1 partial (real reduce-only partial, broker == local == remaining, SL child resized, stop still covers the remainder), tightens the runner stop behind a confirmed structure swing (real modify, tighten-only: a looser candidate moves nothing, the manager's own tighten gate sends no job for a widening, `ModifyStopJob(wider)` is denied), and closes the runner on structure failure through `_flatten` (broker 0, registry `CLOSED`, `EXIT_ENGINE_STRUCTURE`) |
| `b2` REVERSION + broker TP | `entry_profile_row_broker_tp`, `broker_stop_and_far_tp`, `engine_leaves_target_to_broker_tp`, `engine_structure_failure_full_close`, `clean_state_no_halt`, `restart_reconcile` | a REVERSION row carries a broker TP at the target level T and the engine's single full-close stage at the SAME T (Lane Y design, Opus MEDIUM-1); T is far from the market so the broker TP cannot fill by itself. Lane V MEDIUM-1 design: with the broker TP at T the engine does NOT close at T (a market close would race the broker fill): injected quote at T -> no engine close, `stage_left_to_broker_tp` counted, position / TP / SL unchanged, no halt / failure count / stray order. The thesis-failure exit stays with the engine: injected structure failure (broker TP still far away) -> ONE real full reduce-only close (`EXIT_ENGINE_STRUCTURE`); afterwards broker flat, TP gone (no stray order, none in the Nautilus cache), registry terminal, no halt / `flatten_failed` counter / manager error counter |
| `b3` restart with an open position | `entry_profile_row`, `broker_protective_stop`, `engine_tp1_partial`, `volume_broker_eq_local`, `protection_after_partial`, `restart_with_open_position`, `engine_no_refire_after_restart`, `restart_path_tighten_only`, `flatten_verified_path`, `final_restart_reconcile` | orderly stop with the position open (protected by the broker SL), NEW stack on the same state dir: `RECONCILED`, same ticket / volume / broker SL, position adopted by the SAME intent id with the frozen profile, exit plan, registry stop and `exit_state` (`stages_completed` >= 1); the TP1 stage does not re-fire; the stop tightens through the adopted-position path (no local stop child -> broker-verified protect path), loosening is refused; flatten through `_flatten`; second restart clean |

`b2b` (broker TP fills first, then the engine close) cannot be forced on a real broker; it is a fake-broker test only
(`test_b2b_*`, two race variants against the step-4 thesis-failure close, plus `test_b2_broker_tp_fill_closes_the_row_as_a_target_exit` for the
broker-side TP fill booked as `TARGET`). With Lane V's success semantics (broker truth: own magic flat = success) a close that reaches the
broker after its TP filled is harmless: no halt, no flatten-failure count, no stray order, registry terminal.

### What "injection" means

A real market will not conveniently reach TP1 / structure levels inside a ~1 minute canary.  So, ONLY while `manage_exits` runs,
the canary replaces the two readers the manager uses to feed the engine: `stack.bar_source.latest_quote` (the quote TP stages and
stops are evaluated against) and `stack.bar_source.m5_frame` (the closed bars the structure trail / structure failure derive from).
The injected bars are synthetic, strictly post-entry, with exactly one confirmed fractal swing low (and, for the failure, a last
close below it); the injected quote sits at the TP1 / target level.  Everything after the decision is production code: the manager
routes the decision to the real `ReduceJob` / `ModifyStopJob` / `_flatten`, which read the REAL broker quote and send real
reduce-only / modify requests at the real market.  The injection never alters an order price (the fake-broker tests assert it on
the broker's request log: no request price / stop equals an injected value, every order price is the real market).  The readers are
restored in a `finally` after each call.  **No production code was changed**: the injection point is the instance-attribute
override on the canary's own stack object.  Every injected value is printed as `INJECTED_FOR_ENGINE_EVALUATION_ONLY` and stored in
the report (`injected_values`, and `steps[n].injected`) with the real bid/ask at that moment, how often the engine read the
injected inputs (a step with zero reads FAILS: no evaluation happened) and the manager's resulting log entries.

### Live commands (lead)

```
# static plan of every scenario, no broker at all
uv run python scripts/e2_broker_canary.py --dry-run-plan --scenario all --plan-price 1.17

# read-only against the real terminal: refusals + calibrated sizes for every scenario, places nothing
uv run python scripts/e2_broker_canary.py --live --dry-run-plan --scenario all

# one scenario
uv run python scripts/e2_broker_canary.py --live --scenario b1 --confirm-demo-canary=I-AUTHORIZE-ACTIVTRADES-DEMO-CANARY-ONLY --expected-account-hash <hash>

# everything, sequentially (base, b1, b2, b3); about 5-8 minutes (hard timeout per scenario: 120 s base/b2, 150 s b1, 240 s b3; an explicit --timeout wins)
uv run python scripts/e2_broker_canary.py --live --scenario all --confirm-demo-canary=I-AUTHORIZE-ACTIVTRADES-DEMO-CANARY-ONLY --expected-account-hash <hash>
```

Reports: `artifacts/e2_canary/canary_report_<utc>.json` (base), `..._<scenario>.json` (b1/b2/b3) and `..._all.json` (combined, with
the `flat_checks`).  Exit codes as above.

### New refusals (all scenarios, live)

* `eod_recovery.lock` alive -> refused (an AutoTrader-EodRecovery flatten-only runner is active).
* Berlin wall time 21:40-22:35 (the EOD-recovery task fires 21:45-22:30) -> refused (`outside_eod_recovery_window`); a plan-only run is never refused for the window.

### What the Lane K scenarios still do NOT prove

* Real filling modes beyond EURUSD / IOC-style behaviour; other symbols' stops / freeze levels (the EURUSD freeze level is 0).
* Multi-deal / partially filled ENTRY fills (the canary asserts one fill of exactly 2 x min lot).
* A true broker-TP-vs-engine-close race on a real broker (the TP is deliberately placed where it cannot fill; B2b is fake only).
* The engine's decisions on REAL structure: bars and quote are injected for the evaluation (the decision logic is the production
  engine; whether real charts produce good levels is a strategy question, not an execution-contract one).
* Profile routing from a real signal (the attribution is built from the route, not from a scanner signal) and multi-tranche netting.
* A restart within the same wall-clock second as the last reduce order: observed on the fake broker, the new session's first
  reduce-only order then collides with Nautilus' `O-<datetime>-...-<count>` id and is denied
  `DUPLICATE_CLIENT_ORDER_ID_ALREADY_RECORDED` (`_flatten` then latches `flatten_failed`).  A real restart takes seconds, so b3
  waits 1.1 s before stopping; this is reported as an observation, not exercised.
