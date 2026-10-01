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
