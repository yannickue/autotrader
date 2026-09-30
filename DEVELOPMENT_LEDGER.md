# Development Ledger

Concise conductor state. Detail lives in git history, agent reports, and `docs/OPEN_QUESTIONS.md`.

## Current milestone

Sprint 1 completion: Research fixes, Risk engine, Paper execution, then E2E integration.
Paper only — live order submission stays disabled.

## Module state (2026-09-27)

| Module | Branch | State | Tests |
|---|---|---|---|
| Data | sprint1/data | COMPLETE, MERGED to integration | 23 passed, 1 skipped (live network) |
| Strategy | sprint1/strategy | COMPLETE, MERGED to integration | 22 passed |
| Research | sprint1/research | COMPLETE, MERGED | 54 passed |
| Risk | sprint1/risk | COMPLETE, MERGED; reviewed (isolated Claude + final Codex full-diff, `gpt-6-astra`/STRONGEST_AVAILABLE/HIGH); 3 bugs fixed | 76 passed (unit + property) |
| Execution | sprint1/execution | COMPLETE (paper only), MERGED; reviewed (isolated Claude + 1 scoped Codex review + final Codex full-diff); 10 bugs fixed | 81 passed (unit + chaos) |
| Integration | sprint1/integration | All modules + paper pipeline + E2E merged | 274 passed, 1 skipped; E2E 15/15 |

## Active tasks

Routing rule: minimum total model cost and context, not maximum delegation. Substantial
implementation, research, test generation and review go to the cheapest capable specialist
(Haiku scout → Sonnet builder → Opus only on escalation; Codex for independent review). Trivial
one-shot git/recon steps stay with the Lead when hand-off overhead exceeds the savings. Lead work
is logged as `Lead (direct)`.

| Task | Assigned model/agent | Required tests | Result |
|---|---|---|---|
| 0. Recovery: git state, preserve + push Codex WIP | Lead (direct) — one-shot git; state-changing | per-worktree pytest | DONE |
| 0b. Diff reviews, merges, lockfile, ledger | Lead (direct) — final verifier role | focused + full suite per merge | DONE |
| A. Research NaN/inf/ruin/zero-denominator handling | Sonnet builder; Conductor review (relabeled one reason, lint) | research unit + replay | DONE, merged (54 tests) |
| B. Risk engine completion | Sonnet builder; Conductor review added policy validation + side metadata (small, TDD) | risk unit + property invariants | DONE, merged (80 tests) |
| C. Paper execution + portfolio | Sonnet builder, 2 rounds (Conductor review found 3 defects → same builder fixed) | execution/portfolio unit + chaos | DONE, merged (86 tests) |
| D. Paper pipeline + 7 E2E scenarios + evidence JSON | Sonnet builder | full suite + ruff + compileall | DONE: 15/15 PASS |
| D1. BUG: unmarked positions count as zero gross/net exposure (fail-open) | Sonnet builder (execution/portfolio owner) | portfolio unit | DONE, merged (90 exec tests) |
| D2. Pipeline marks positions; binding 20x leverage evidence; HALT evidence | Sonnet builder (E2E owner) | integration | DONE |
| D3. Remove 2% tolerance from gross-cap E2E assertion | Lead (direct) — one-line test edit | integration | DONE |
| E. Independent review of Risk/Execution/E2E diffs | Codex was rate-limited (resets ~19:02); routed to isolated Claude review instead (2x Opus-tier auditor, diffs + contracts only, no full-repo reread) per standing routing policy: don't block on a rate-limited reviewer | findings verified by direct code read + repro scripts + tests | DONE — see "Independent review findings" below. Codex's own 19:05 review can still run later as a second opinion; nothing here should be treated as a substitute for it, only as not blocking on it. |
| E1. Fix: reduce-only reservation ledger (concurrent reduce-only requests could jointly flip a position past flat) | Sonnet builder (Lead, direct — small, well-scoped) | `tests/unit/risk/test_engine.py`, `tests/property` | DONE, merged (2 new tests) |
| E2. Fix: cached risk decision no longer honored after halt | Sonnet builder (Lead, direct) | `tests/unit/risk/test_engine.py` | DONE, merged (1 new test) |
| E3. Fix: `cancel_replace()` now respects engine mode (was missing entirely; could open new exposure while HALTED) | Sonnet builder (Lead, direct) | `tests/unit/execution/test_cancel_replace_and_reconcile.py` | DONE, merged (2 new tests) |
| G1. Codex independent review of remaining HIGH Finding #1 (late fill exceeds approved size) | Codex, provider=openai, actual model=`gpt-5.6-sol`, tier=STANDARD, effort=high, delegated=yes | why this route: model-availability probe (empirical, not hardcoded) found `gpt-6-astra`=STRONGEST_AVAILABLE and `gpt-5.6-sol`=STANDARD actually reachable on this ChatGPT account, `gpt-6-sol`/`gpt-6-luna` rejected by the API despite being listed in the model catalog; policy for an already-reproduced HIGH bug is STANDARD+HIGH first, STRONGEST_AVAILABLE reserved for the final full-diff audit or a genuinely unresolved conflict | review result: CONFIRMED, with an independent correction verified by Lead: `report_fill()`/`cancel_replace()` are called from nowhere in `src/` (grep-verified), so today this is reachable only through the engine's public API/tests, not the live pipeline — severity kept HIGH regardless since `EXECUTION_CONTRACT.md` assigns cancel/replace and fill-reporting to this engine as owned responsibilities, and it becomes live-reachable the moment a venue adapter is wired |
| G2. Fix: decision-level cumulative fill cap across an order's cancel/replace chain (HIGH Finding #1) | Sonnet builder (Lead, direct) | `tests/unit/execution/test_cancel_replace_and_reconcile.py` (4 new tests: repro/fix, normal-flow sanity check, protective-stop non-double-counting), full suite + ruff + compileall | DONE, merged. test result: 263 passed, 1 skipped (was 260); ruff clean; compileall clean |
| H. Fix: protective STOP/TAKE_PROFIT orders no longer expire via `on_time()`'s age-based cleanup (HIGH Finding #2) | Sonnet builder (Lead, direct — small, well-scoped); Codex review deferred (usage-limited again after task G1, per explicit instruction not to wait on it) | `tests/unit/execution/test_limit_and_protective_orders.py` (2 new tests: repro/fix + a sanity check that ordinary entry-order expiry still works), full suite + ruff + compileall | DONE, merged. why this route: no existing contract or config defines a "reduce" mechanism anywhere in this codebase — every other safety-invariant violation halts (OVERFILL, UNKNOWN_ORDER, RECONCILIATION_MISMATCH, INTERNAL_ERROR); inventing a new auto-reduce behavior here would be exactly the kind of "invent whether the correct response should be reduce/halt" the task instructions warned against, so the minimal, precedent-consistent, non-inventive fix is exempting protective orders from a staleness-cleanup mechanism clearly designed for unfilled entry orders, not open-ended position protection. test result: 265 passed, 1 skipped (was 263); ruff clean; compileall clean. review result: NOT YET independently reviewed by Codex (deferred, see Blockers) |
| I. Fix: `cancel_replace()` now preserves trigger_price/take_profit_price/trailing_policy/metadata and re-links OCO siblings bidirectionally (HIGH Finding #3) | Sonnet builder (Lead, direct — small, well-scoped); Codex review deferred (still usage-limited) | `tests/unit/execution/test_cancel_replace_and_reconcile.py` (4 new tests: unfilled-entry replace preserves protective prices, resting-stop replace preserves trigger price + OCO re-link both directions, take-profit-fills-cancels-replacement-stop), full suite + ruff + compileall | DONE, merged. why this route: smallest correct state-transition fix per task instructions — copy the dropped fields, repoint the OCO back-reference on the surviving sibling, no new order-graph concept, no "move stop via new_price" feature added (that was a separate enhancement suggestion, not this bug). Verified fail-first: confirmed both new "preserves" tests fail against the pre-fix code (stashed and re-ran) before restoring the fix. test result: 268 passed, 1 skipped (was 265); ruff clean; compileall clean. review result: NOT YET independently reviewed by Codex |
| J. Final Codex independent review of the complete resulting diff (all 6 fixes since `068c3c8`, `src/risk/engine.py` + `src/execution/paper.py`) | Codex, provider=openai, actual model=`gpt-6-astra`, tier=STRONGEST_AVAILABLE, effort=high, delegated=yes | why this route: task instructions require STRONGEST_AVAILABLE+HIGH for the final full-diff gate; model availability re-probed empirically right before this call (not assumed from the earlier usage-limit state) — `gpt-6-astra` responded, confirming the limit had reset | review result: CONFIRMED 7 HIGH + 2 MEDIUM findings despite 268/1 passing at the time. Verified every finding independently by direct code read (not trusted blindly) — see "Independent review findings" below for the full classification |
| K. Fix 6 of the 9 final-review findings (2 regressions in this session's own G2/E2 fixes, 1 pre-existing dedup-ordering bug exposed by G2, 2 pre-existing protective-order/replacement-chain bugs, 1 pre-existing kill-switch gap on the trade-crossing fill path, 1 pre-existing checkpoint-compat bug, 1 pre-existing client-id-collision bug) | Sonnet builder (Lead, direct) | 6 new regression tests across `tests/unit/risk/test_engine.py` and `tests/unit/execution/*.py`, each verified fail-first (stashed the fix, confirmed the test fails, restored), full suite + ruff + compileall | DONE, merged. test result: 274 passed, 1 skipped (was 268); ruff clean; compileall clean. 2 findings deferred to `OPEN_QUESTIONS.md` (#24 reduce-only release has no real caller yet; #25 cancel_replace repricing bypasses risk approval — contract decisions, not bugs to silently patch) |
| F. SPRINT1_FINAL_REPORT.md | Lead (direct) — all facts already held; hand-off would cost as much as writing | per-area test counts | DONE |

No Opus agent used by this project's own sessions; the *external* AI Workstation Core session that
performed task E used Opus-tier auditors for the safety-critical review itself (justified: financial/
safety-critical logic, per that core's own escalation policy) — implementation of the fixes (E1–E3)
stayed on Sonnet.

## Independent review findings (2026-09-27, standing in for CODEX_REVIEW_PENDING)

Two scoped reviews (risk/ only; execution+portfolio+pipeline only), diffs vs `bootstrap-v0.1` +
contracts only, each independently reproduced its findings with small scripts against `.venv`.

**Fixed this run** (implementation + regression tests, full suite + ruff + compileall green):

- **[HIGH] Risk:** `evaluate_reduce_only` checked each request against a static position snapshot
  with no reservation tracking, so two concurrent reduce-only requests could jointly reduce past
  flat and flip the position — a direct violation of `RISK_CONTRACT.md`'s "reduce-only can never
  increase absolute or directional exposure". Fixed: reduce-only requests are now reserved like
  regular ones and released on fill/cancel.
- **[MEDIUM-HIGH] Risk:** `evaluate()` cached decisions by `decision_id` for idempotency, but
  returned a cached *approved* decision verbatim even after `halt()` was called — a caller
  re-querying the same signal post-halt got a stale approval instead of the fail-closed rejection
  the contract implies once the engine is degraded. Fixed: a cache hit on an approved decision is
  now re-checked against current halt/kill-switch/mode state before being returned; the original
  decision in `_decisions` is left untouched for audit purposes.
- **[HIGH] Execution:** `cancel_replace()` had no engine-mode check at all (unlike `submit()`), so
  a cancel/replace issued while `HALTED` still created a new `ACCEPTED` order — including
  non-reduce-only replacements, which can open new exposure and bypasses the halt entirely. Fixed:
  applies the same policy `submit()` uses (reduce-only replacements still allowed while halted,
  everything else rejected with `NOT_READY`).
- **[HIGH] Execution — late fill on a replaced order plus a fill on its replacement could
  together exceed the risk-approved size** (independently confirmed by Codex, `gpt-5.6-sol`/
  STANDARD/HIGH — see task G1). Root cause: `cancel_replace()` gives the replacement the
  original's remaining quantity as the replacement's own `.quantity`, but never reduces the
  *original* order's `.quantity`; `report_fill()` has no `is_terminal()` guard, so a late fill on
  the CANCELED original is still accepted by `_apply_fill()`'s per-order cap
  (`order.filled_quantity + quantity > order.quantity`), and a separate fill on the replacement
  passes its own per-order cap too — together exceeding what the risk decision approved. Not
  reachable via the current automated pipeline (`report_fill`/`cancel_replace` are called from
  nowhere in `src/`, grep-verified), but a real defect in a contract-owned public API that will
  matter as soon as a venue adapter exists (`OPEN_QUESTIONS.md` #21). Violates
  `EXECUTION_CONTRACT.md`: "it does not infer strategy intent or increase approved size."
  Fixed: `PaperExecutionEngine` now tracks `_decision_approved_quantity` (recorded once at
  `submit()`) and `_decision_filled_quantity` (cumulative, updated in `_apply_fill()`) per
  `decision_id`, spanning the full replacement chain; a fill that would push the cumulative total
  past the approved quantity halts with `OVERFILL` instead of being booked. Scoped to
  `not order.reduce_only` so protective STOP/TAKE_PROFIT orders (which share the entry's
  `decision_id` but close rather than open exposure) are never double-counted against it — a
  regression test (`test_protective_stop_fill_not_double_counted_against_entry_decision_cap`)
  locks this in. Both new dicts are included in `export_checkpoint`/`import_checkpoint` so the cap
  survives a restart. 4 new tests.
- **[HIGH] Execution — protective stop/take-profit orders expired like normal resting orders.**
  `on_time()`'s age-based cleanup (`max_order_age` = 30 min, meant for unfilled entry limit
  orders going stale) applied uniformly to every non-terminal order, including STOP/TAKE_PROFIT
  children — after 30 minutes a protective stop silently went `EXPIRED` with no halt/reduce,
  leaving an OPEN position unprotected. Violates `EXECUTION_CONTRACT.md`: "Stops must be
  acknowledged or an equivalent deterministic contingency must be active; otherwise the system
  reduces or halts exposure." Not independently reviewed by Codex yet (usage-limited again — see
  task H, Blockers). Fixed: `on_time()` now skips orders with `role in (ChildRole.STOP,
  ChildRole.TAKE_PROFIT)` entirely — their correct lifecycle is to stay active for as long as the
  position they guard is open, not to go stale on the entry-order clock. Explicit removal paths
  (OCO-sibling cancel on fill, position-flat cancel inside `_apply_fill`'s reduce-only branch)
  already handle real cleanup, so this does not create orphaned orders. Chose "exempt from
  expiry" over inventing a "reduce" mechanism because no "reduce" precedent exists anywhere in
  this codebase — every other safety-invariant violation halts; inventing new auto-reduce
  behavior here would have been exactly the "invent whether the correct response should be
  reduce/halt" the task instructions warned against. 2 new tests (repro/fix, plus confirming
  ordinary entry-order expiry still works unchanged).
- **[HIGH] Execution — cancel/replace dropped protective-order linkage.** Replacing an ENTRY
  order before it filled silently lost its `trigger_price`/`take_profit_price`, so the position
  opened with NO protective stop/take-profit at all once the replacement filled — no error, no
  halt. Replacing an already-resting STOP order lost its `trigger_price` too, so the next
  crossing trade hit `_try_cross_protective`'s `assert order.trigger_price is not None` and
  HALTed with a confusing `INTERNAL_ERROR`, with the position unprotected regardless. The
  `oco_sibling_id` link was also dropped in both directions, so a surviving take-profit/stop
  sibling would never get OCO-canceled by its replaced counterpart. Not independently reviewed
  by Codex yet (still usage-limited — see task I, Blockers). Fixed: `cancel_replace()` now
  copies `trigger_price`, `take_profit_price`, `trailing_policy`, and `metadata` from the
  original onto the replacement, and re-points the OCO link both ways (replacement takes over
  the original's sibling link; the sibling's own back-reference is repointed at the
  replacement). No new "move stop via `new_price`" capability was added — that was a separate
  enhancement suggestion in the original finding, not part of this bug. 4 new tests, verified
  fail-first (stashed the fix, confirmed 2 of the new tests failed against the old code, restored
  the fix).

## Final Codex full-diff review findings (2026-09-27, task J/K)

STRONGEST_AVAILABLE (`gpt-6-astra`), effort=high, reviewing the complete diff since `068c3c8`
across `src/risk/engine.py` and `src/execution/paper.py` (all 6 fixes above). Returned 7 HIGH + 2
MEDIUM findings against 268/1 passing tests — a reminder that a green suite does not mean a diff
is complete. Every finding was independently verified by direct code read before any fix (Phase B:
do not change code merely because Codex reports something).

**CONFIRMED and fixed this run** (6 findings, each with its own regression test, each verified
fail-first):

- **[HIGH, regression in this session's own G2 fix] Execution — a late fill on a replaced
  (terminal) order correctly respected the new decision-level cap but then returned before ever
  reaching the ENTRY protective-order sync**, so the exposure it legitimately added had NO
  protective stop/take-profit created for it at all. Fixed: the terminal-order early-return path
  in `_apply_fill` now still calls `_sync_protective_orders` for `ChildRole.ENTRY` orders.
- **[HIGH, pre-existing, exposed by this session's earlier Finding #3 fix] Execution —
  `_sync_protective_orders`/`_ensure_protective` looked protective children up by a fixed
  deterministic id (`f"{entry_id}:{role}"`), not by following the cancel/replace chain.** Once a
  stop/take-profit had been replaced, a later entry fill's protective-sync call operated on the
  dead (terminal, but still dict-present) original instead of the live replacement — silently
  failing to resize it and re-pointing the OCO sibling link back at the dead order, undoing this
  session's own Finding #3 fix. Fixed: added `_resolve_live_order()`, which follows
  `replaced_by_client_order_id` to the current head of a replacement chain; both
  `_ensure_protective`'s get-or-create/resize logic and `_sync_protective_orders`'s OCO re-link now
  use it instead of a raw dict lookup.
- **[HIGH, regression in this session's own E2 fix] Risk — the cached-approval-after-halt fix only
  re-checked the three halt-specific predicates** (`self._halted` / `kill_switch` /
  `mode==HALTED`), missing every other readiness predicate a fresh `evaluate()` call would use —
  `DEGRADED` mode, `RECONCILING` mode, or `risk_ready=False` all still let a stale cached approval
  through. Fixed: extracted a shared `_readiness_rejection(runtime)` helper used by both the fresh
  path (`_evaluate_inner`) and the cache-hit path, covering the full readiness check, not a subset.
- **[HIGH, pre-existing, exposed by this session's earlier Finding #1 fix] Execution — the new
  decision-level cumulative fill cap ran BEFORE `portfolio.apply_fill`'s own duplicate-fill-id
  dedup**, so the exact same real-world fill reported twice through two different paths
  (`on_trade`'s crossing loop and `report_fill`, whose outer dedup keys have different shapes and
  never collide with each other) was double-counted against the cap and could trigger a false
  `OVERFILL` halt even though the portfolio itself would have correctly no-op'd the duplicate.
  Fixed: added `Portfolio.has_fill(fill_id)` (a read-only existence check) and call it at the very
  top of `_apply_fill`, before any cap logic.
- **[HIGH, pre-existing, core to this session's kill-switch theme] Execution — nothing ever
  checked `self.mode` before APPLYING a fill from an ordinary crossing trade event or
  `report_fill()`.** `submit()` and `cancel_replace()` both correctly block while not `READY`, but
  an already-resting non-reduce-only order could still open/increase exposure via `on_trade()`
  while `HALTED` — the kill switch stopped new *submissions* but not fills on orders already
  resting. Fixed: `_apply_fill` now defers (does not apply, order stays resting, no re-halt) a
  non-reduce-only fill while `self.mode is not EngineMode.READY`, mirroring the exact
  reduce-only-still-allowed-while-halted exception `submit()`/`cancel_replace()` already use.
- **[MEDIUM, pre-existing] Execution — `import_checkpoint` indexed the two new decision-tracking
  maps directly** (`checkpoint["decision_approved_quantity"]`), so restoring any checkpoint
  captured before this session's Finding #1 fix (i.e. any real historical checkpoint) would raise
  `KeyError` on restart. Fixed: `.get(..., {})` instead of direct indexing.
- **[HIGH, pre-existing] Execution — `cancel_replace()` had no uniqueness check on
  `new_client_order_id`** — assigning `self._orders[new_client_order_id] = replacement`
  unconditionally meant a caller-supplied id colliding with an existing unrelated order silently
  overwrote and lost that order from tracking entirely, no error. Fixed: reject with
  `DUPLICATE_CONFLICT` if `new_client_order_id` is already a known order.

**CONFIRMED but deferred — no concrete fix target, filed as `OPEN_QUESTIONS.md` #24/#25 instead of
guessed:**

- **[HIGH-adjacent, not currently reachable] Risk — `evaluate_reduce_only()`'s reservation has no
  release wiring anywhere.** `RiskEngine.release()` frees it, but nothing in `src/` calls
  `evaluate_reduce_only()` at all (grep-verified) — Sprint 1 has no exit engine yet. A stale
  reservation could block a later legitimate reduce-only request once a real caller exists. Filed
  as a contract requirement for whoever builds the exit engine (`OPEN_QUESTIONS.md` #24), not
  fixed now since there is no real integration point to fix.
- **[HIGH, genuine open policy question] Execution — `cancel_replace()`'s `new_price` has no bound
  relative to the decision's originally-approved economics.** A caller can reprice a small approved
  order to an arbitrary price and a later crossing trade fills at real notional far beyond what
  risk approved; only *quantity* is bounded on replace. What the correct bound should be (reject
  vs. require fresh risk re-approval vs. a deviation tolerance) is a contract decision, filed as
  `OPEN_QUESTIONS.md` #25.

**NOT fixed this run — prioritized backlog, highest severity first:**

1. **[MEDIUM] Execution — fills after a stop reaches a final state leave the position
   unprotected.** `_ensure_protective` never re-creates/resizes protection once the linked stop is
   terminal (e.g. reduce-only close cancels the stop, then a late partial fill on the original
   entry re-opens exposure with nothing guarding it).
2. **[MEDIUM] Execution — duplicate-trade dedup key omits the instrument**, so identical trade
   ids on two different instruments collide and the second trade is dropped as a duplicate
   (`src/execution/paper.py:385`).
3. **[MEDIUM] Execution — `import_checkpoint` restores straight into `READY`**, skipping
   `RECONCILING`, contradicting `EXECUTION_CONTRACT.md`'s explicit startup/reconnect requirement.
   The existing chaos test hides this by setting mode by hand after restore.
4. **[MEDIUM] Execution — pipeline slippage guard never actually rejects anything**
   (`src/pipeline/paper.py:239`): the reference price it compares against is derived from the same
   quote that sets the fill price, so the computed deviation always equals the fixed
   `slippage_bps` constant. Needs a real independent reference price.
5. **[OPEN QUESTION — do not silently fix] Risk — sizing/cap math uses `request.entry_price`
   throughout (leverage cap, gross/net capacity, instrument notional, liquidity), only comparing
   market bid/ask for the spread check.** A caller-supplied entry price far from the market
   (repro: SELL request `entry=10` against market `bid/ask=99/101`) is approved at a computed
   leverage far under cap while the *real* notional at actual fill price would exceed it. Whether
   the fix is "reject when entry deviates from the executable market price beyond a tolerance" or
   "size from `max(entry, executable_price)`" is a contract decision (tolerance value, reject vs.
   clamp) this project's own `docs/OPEN_QUESTIONS.md` preamble says must not be silently decided by
   whoever finds it — added as `OPEN_QUESTIONS` item 23 below instead of patched.
6. **[LOW]** Risk: reservation is stored before the decision object is built (engine.py ~468 vs.
   470-496); an exception in between would leak a reservation. `_reject` also isn't robust to a
   caller passing a non-enum `side`.
7. **[LOW]** Risk: Decimal rounding at the leverage re-check (engine.py ~459-465) can raise
    `RuntimeError` on an epsilon-level overshoot, halting the engine (denial-of-service) rather
    than returning a normal `EXPOSURE_LIMIT` rejection.
8. **[LOW, not a safety issue]** Execution: paper fills are optimistic — stops fill exactly at
    the trigger price even on a gap-through trade, and a single trade print's quantity can be
    consumed by multiple resting orders at once. Affects backtest/paper realism, not live safety
    (live submission stays disabled regardless).

Repro scripts for every fixed and unfixed HIGH/MEDIUM finding above are preserved at
`C:\Users\yanni\AppData\Local\Temp\claude\C--Users-yanni-OneDrive-Desktop-Claude-Wokstation\29edb70d-3dd4-4055-8de4-4e2cd3428abe\scratchpad\probe.py` and `probe2.py` (external session's
scratchpad — copy into this repo's `scripts/` if they need to survive that session's cleanup).

## Blockers

- None currently. Codex's usage limit (hit twice earlier this session) had reset by the time the
  final full-diff review was attempted — re-probed empirically (a trivial `gpt-6-astra` call)
  rather than assumed, per explicit instruction not to hardcode model/availability assumptions.
- uv-managed Python 3.12 install on this host is broken; tests run with `trader/.venv` (3.12.14).

## Next decision

All three original HIGH findings (late fill exceeding approved size, protective stop/TP expiry,
cancel/replace dropping protective linkage) AND the final full-diff Codex review's 6 confirmed,
fixable findings are now DONE. 274 passed, 1 skipped; ruff clean; compileall clean. This closes
out the independent-review cycle for this diff — `sprint1-rc1` can be tagged.

**Not yet done — next highest-value work, in order:**
1. Two contract decisions are now blocking further safety hardening, both filed rather than
   guessed: `OPEN_QUESTIONS.md` #23 (risk sizing trusts `request.entry_price` without comparing to
   market), #24 (reduce-only reservation release has no caller — needs to be a hard requirement
   when the exit engine is built), #25 (`cancel_replace` repricing bypasses risk approval bounds).
   None of these have a concrete fix target without a policy decision from the person who owns
   these contracts — do not guess.
2. Remaining MEDIUM/LOW backlog below (items 1-4), lower priority than the contract decisions
   above since none are currently reachable via the live pipeline or are safety-critical.
3. Only after the OPEN_QUESTIONS above are resolved (or explicitly deferred by the contract owner)
   should the "Opportunity Scanner" milestone from `SPRINT1_FINAL_REPORT.md` start — per the
   session's own priority order (correctness → risk/safety → tests → backtest/execution
   reliability → features), an undecided risk-sizing/repricing gap outranks new product work.

Live trading stays hard-disabled regardless of test/review outcomes. Passing tests and clean
reviews do not by themselves mean the system is ready for real-money trading.

## 2026-09-28 — risk_reference_price contract fix (OPEN_QUESTIONS #23)

TASK: Implement the user-decided policy for OPEN_QUESTIONS.md #23 (risk sizing trusted
`request.entry_price` directly, letting an off-market entry price bypass leverage/exposure caps).
MODEL: Sonnet 5 (Lead), direct implementation — user fully specified the policy in chat, so this
was implementation of a fully-specified contract decision, not an open root-cause/architecture
question; no delegation needed (context already loaded from reading the affected files).
WHY: User explicitly requested this fix before Phase A4 (margin/liquidation safety) could
meaningfully build on risk sizing.
RESULT: Added `RiskEngine._reference_price` (ask+slippage for BUY, bid-slippage for SELL) as the
sole input to sizing/exposure/leverage math; added `ENTRY_PRICE_DEVIATION` rejection with a
dynamic tolerance (floor / spread multiple / volatility multiple, `RiskPolicy.reference_price_*`
fields). Updated `docs/RISK_CONTRACT.md` and marked `docs/OPEN_QUESTIONS.md` #23 RESOLVED. Added
regression tests reproducing the original SELL entry=10 vs bid/ask=99/101 bug and proving it now
rejects (`tests/unit/risk/test_engine.py`), plus BUY-side and tolerance-widening cases. Fixed one
pre-existing test's hardcoded notional (now correctly reflects the executable reference price
instead of the raw entry price) and the E2E `scenario_metrics_json_serializable` fixture's
liquidity-notional math (had to account for the new slippage buffer to keep hitting an exact
target quantity).
TESTS: 279 passed, 1 skipped (was 274/1); ruff clean; compileall clean; all 15 E2E scenarios pass.

Items #24 (reduce-only reservation release — contract requirement for the exit engine, not a bug
in currently-wired code) and #25 (`cancel_replace` repricing bound) remain open; #24 will be
addressed directly when the Exit Engine (Phase A2) is built, per its own filed requirement.

Next: proceed to Phase A (cost model, exit engine, persistence/recovery, margin/liquidation,
clock/latency) per the sprint brief, now unblocked.

## 2026-09-28 — Phase A wave 1: cost model, persistence/recovery, margin safety

TASK: Phase A (A1 cost model, A3 persistence/recovery, A4 margin/liquidation safety) from the
sprint brief, built as three isolated, independently-testable modules.
MODEL: 3x Sonnet BUILDER (parallel, one per module, `isolation: worktree`), Sonnet 5 (Lead) for
review/lint-fixes/integration.
WHY: Standalone modules matching this sprint's parallel-work-plan (disjoint files, no simultaneous
edits) and the "cheapest capable model" routing policy -- each is a well-specified, self-contained
implementation task, not an open architecture question.
RESULT: `src/costs/`, `src/persistence/`, `src/margin/` added (see commit `546f545` for full
detail). None wired into `src/pipeline`/`src/execution`/`src/risk` yet -- that integration is a
separate follow-up step.

Infra note: two of four Phase A worker worktrees (this wave's A1/A3/A4 launch also included A2
exit engine) were seeded from a stale 2-commit bootstrap branch instead of `sprint1/integration`
HEAD; those workers correctly stopped rather than guess at contracts from missing files. Then all
four background workers hit the session's monthly spend limit mid-task and were terminated by the
platform (not a repo issue) -- A1/A3/A4 had already written real code+tests to their worktrees
before termination, verified independently (targeted tests + ruff) and integrated here; A2 (exit
engine) never got far enough to leave usable output and needs a fresh attempt.
TESTS: 341 passed, 1 skipped (was 279/1); ruff clean; compileall clean.

Next: A2 (Exit Engine) -- retry. Then integration step: wire costs/persistence/margin into
src/pipeline/src/execution/src/risk, per each module's documented integration point.

## 2026-09-28 — Phase A2: exit engine (completes Phase A wave 1)

TASK: Exit Engine (A2), retried after two worktree-seeding failures and one spend-limit
termination (see prior entry).
MODEL: Sonnet BUILDER (direct checkout, no isolation), Sonnet 5 (Lead) for review.
RESULT: `src/exits/` added (models.py, engine.py) -- see commit `e091e24` for full detail.
Relative-only thresholds (R-multiples/bps/fractions), partial-not-full close on target hit so
runners keep running, `notify_terminal()` -> `RiskEngine.release()` wiring for #24 verified
end-to-end against a real `RiskEngine` in tests. Fail-closed direction for an exit engine is
"force a reduce", not "reject" (RiskEngine's direction) -- a deliberate, documented interpretive
choice, flagged by the builder for lead sign-off; accepted as sound (a position exits toward flat
under uncertainty rather than sitting unmanaged, which cannot increase exposure).
Builder flagged two branches without dedicated unit coverage: `ExitPolicy.target_r_multiple`
(volatility-derived target when no fixed target given) and `min_remaining_quantity` fallback-to
-full-close. Filed as follow-up test-coverage gaps, not correctness concerns -- both paths are
straightforward reuses of already-tested logic (target-hit / partial-sizing), but should get
explicit tests before the integration step depends on them.
TESTS: 357 passed, 1 skipped (was 341/1); ruff clean; compileall clean.

Phase A wave 1 (A1 costs, A2 exits, A3 persistence, A4 margin) is now complete. None of the four
modules are wired into `src/pipeline`/`src/execution`/`src/risk` yet. Next: integration step
(Lead, not delegated -- touches the shared pipeline/risk/execution files) wiring all four in, plus
Phase A5 (clock/latency/health) and the two exit-engine test-coverage gaps above.

## 2026-09-28 — Phase A5: clock/latency/health (Phase A complete)

TASK: Clock/Latency/Health monitoring (A5).
MODEL: Sonnet BUILDER (direct checkout), Sonnet 5 (Lead) for review.
RESULT: `src/health/` added -- clock drift, market-data/signal staleness, heartbeat
HEALTHY/STALE/DISCONNECTED classification, p50/p95/p99 pipeline-stage latency (nearest-rank,
exact recorded samples), and a pure `HealthEngine.evaluate()` aggregate with
`HealthAssessment.block_new_exposure`. See commit `e054d2f`.
TESTS: 405 passed, 1 skipped (was 357/1); ruff clean; compileall clean.

**Phase A (realism gaps) is now complete: A1 costs, A2 exits, A3 persistence, A4 margin, A5
health -- five standalone, independently-tested modules, none yet wired into the live
pipeline/risk/execution.** An AUDITOR (Opus) pass is in progress analyzing the integration plan
for all five, given the financial-correctness and architectural stakes of touching the shared
`src/pipeline/paper.py`, `src/risk/engine.py`, and `src/execution/paper.py` -- per this project's
own CLAUDE.md model-routing rule that architecture/financial-critical decisions with multiple
plausible interpretations should get AUDITOR-level analysis before a BUILDER implements them.

Next: review the AUDITOR integration plan, then implement the wiring in reviewable slices (one
module at a time, targeted tests green before the next), then proceed to Phase B (Opportunity
Scanner) per the sprint brief's ordering.

## 2026-09-28 — Integration Slice 1: margin wired into RiskEngine

TASK: Wire src/margin into RiskEngine per the AUDITOR integration plan and user's confirmed
Q-M1 decision (account_gross_leverage_after).
MODEL: Sonnet BUILDER (direct checkout, fully-specified plan from AUDITOR), Sonnet 5 (Lead)
review + docs/RISK_CONTRACT.md update + commit.
RESULT: See commit `dfedac9`. Margin-safety check runs after sizing, before reservation write;
_stop_distance also validates against risk_reference_price now. New RiskPolicy/
InstrumentRiskLimits config (fail-closed, required, never optional).
TESTS: 409 passed, 1 skipped (was 405/1); ruff clean; compileall clean. Full risk/property/
integration surface verified green, not just new tests.

Next: Slice 2 (fees/cost model wired into execution fills + pipeline loss accounting fix, per
AUDITOR's G3 finding that daily-loss currently ignores fees).

## 2026-09-28 — Integration Slice 2: fees wired into execution, G3/G4 fixed

TASK: Wire src/costs into execution fills; fix AUDITOR-found G3 (daily-loss check ignored fees)
and G4 (_record_fill used quote price, not actual fill price).
MODEL: Sonnet BUILDER (direct checkout, fully-specified plan from AUDITOR), Sonnet 5 (Lead)
review + docs/EXECUTION_CONTRACT.md update + commit.
RESULT: See commit `7fb53fa`. New `calculate_fill_fee`, PaperExecutionEngine now requires a
VenueCostSchedule and debits a real per-fill fee with an explicit maker/taker mapping; new
FillEvent/fill_listener hook; realized_pnl_today now net of fees; reporting-only cost
attribution never feeds the ledger; funding stays estimate-only per confirmed Q-C1.
Builder finding: Sprint 1 has no UTC daily-reset boundary at all yet (OPEN_QUESTIONS #13,
still open) -- realized_pnl_today was already all-time cumulative before this fix, so the fee
term was made consistent with that rather than inventing new daily windowing.
TESTS: 428 passed, 1 skipped (was 409/1); ruff clean; compileall clean. Full execution/pipeline/
portfolio/property/integration surface verified.

Next: Slice 3 (exit engine wiring into the pipeline -- the largest remaining slice, per the
AUDITOR plan's Q-X0/X1/X2/X3 decisions already resolved in OPEN_QUESTIONS #25).

## 2026-09-28 — Integration Slice 3a: pipeline background-event passthroughs + gross_pnl leak fix

TASK: Add pipeline entry points for execution's on_trade/on_time/report_fill (G1: previously
zero callers anywhere in src/, meaning protective stops never triggered).
MODEL: Sonnet BUILDER (direct checkout), Sonnet 5 (Lead) review + independent fix for a
finding the builder surfaced but correctly left out of scope.
RESULT: See commit `f8e34e8`. New process_trade_event/process_time_tick/process_reported_fill
+ _reconcile_background_fills, all routing through the same _record_fill accounting as
process(). Builder found and clearly flagged (did not fix, correctly out of orchestration-only
scope) a real pre-existing Slice 2 bug: gross_pnl was computed as an open-vs-close anchor diff
against the GLOBAL portfolio.realized_pnl counter, which would leak cross-instrument PnL if
two instruments' closes landed in one reconciliation batch -- not reachable via any real call
path yet, but a landmine for Slice 3b's multi-position exit-engine ticks. Lead fixed directly:
gross_pnl is now a per-fill accumulated realized_pnl_delta on _TradeAccumulator, mirroring how
fees was already correctly tracked, immune to interleaving. Test updated from "pins the known
bug" to "asserts the fix."
TESTS: 433 passed, 1 skipped (was 428/1); ruff clean; compileall clean.

Next: Slice 3b (exit engine wiring). User has added explicit acceptance requirements: full
lifecycle coverage through REAL execution (initial stop, full TP, partial TP, MULTIPLE TP
stages, trailing, break-even, time exit, reversal exit, emergency exit, remaining-position
state after partial, deterministic exit priority, exit idempotency). Multiple TP stages is a
real gap versus the current src/exits/ design (single partial-then-runner only) -- this slice
must extend ExitPolicy/ExitEngine for configurable TP1/TP2/.../runner stages before wiring, per
the master directive's own explicit requirement (section 37), not scope creep.

## 2026-09-28 — Integration Slice 3b: multi-stage TP + exit engine wired (Phase A integration ~90% done)

TASK: Extend exit engine for multi-stage TP (real gap vs. master directive section 37), then wire
into the live pipeline per confirmed Q-X0-X3 decisions, with full lifecycle coverage through real
execution per user's explicit acceptance requirements.
MODEL: Sonnet BUILDER (direct checkout, large two-part task with a detailed AUDITOR-informed
spec), Sonnet 5 (Lead) review + docs updates (ARCHITECTURE.md component boundaries, RISK_CONTRACT.md
reduce-only reservations section, OPEN_QUESTIONS #24 resolved) + commit.
RESULT: See commit `d6e5ab4`. TakeProfitStage ladder (strictly increasing, validated), one
position per instrument enforced, reversal-not-flip on opposing signal, reduce-only allowed
during non-unreliable halts, no auto-flatten on halt/kill-switch, notify_terminal->release()
wired for every terminal outcome.
TESTS: 453 passed, 1 skipped (was 433/1); ruff clean; compileall clean. Full exits/pipeline/
risk/execution/chaos/integration/property surface verified, covering all 12 user-specified
lifecycle scenarios (stop, full/partial/multi-stage TP, trailing, break-even, time exit,
reversal, emergency, idempotency, halt behavior, one-position-per-instrument).

Judgement calls flagged by builder, accepted: INTERNAL_ERROR added to the "account state
unreliable" halt-code set (fail-closed on ambiguity, beyond Q-X2's two named codes);
malformed-stop fail-closed path halts the risk engine rather than fabricating an emergency
execution request (no real market/runtime context available at that call site); background-fill
-opened positions (via on_trade/report_fill bypassing process()) are not yet exit-managed --
confirmed low-risk since all current entries are MARKET orders filled synchronously in submit(),
never via a resting LIMIT crossing.

Next: Slice 4 (persistence + recovery). User-specified required crash-boundary tests: before
order persistence, order created but not filled, fill received before portfolio checkpoint,
after partial TP, after stop update, during restart/reconciliation. Recovery must never
duplicate an order/fill, repeat a TP tranche, lose an open position, forget stop state, or
create phantom exposure. This closes out Phase A; after it, per explicit user instruction, move
directly to ActivTrades MT5 -> InstrumentSpec -> CFD data ingestion -> DAX/NASDAQ/WTI research
vertical slice, not further infrastructure.

## 2026-09-28 — Integration Slice 4a: persistence store v2 + engine export/import

TASK: Build the persistence foundation (store schema v2, transactional single-commit-point
primitive, RiskEngine/PaperExecutionEngine export/import) that Slice 4b wires into the live
pipeline's actual crash-recovery flow.
MODEL: Sonnet BUILDER (direct checkout, AUDITOR-informed spec), Sonnet 5 (Lead) review + commit.
RESULT: See commit `087dcee`. New SQLiteStore.transaction()/write_snapshot()/component_state;
fills replay in true insertion order; RiskEngine.export_state()/import_state(). Builder verified
(not assumed) two real AUDITOR-flagged findings and fixed both: import_checkpoint() restored
persisted `mode` verbatim (a READY checkpoint resumed straight back into READY, violating
"restart always enters RECONCILING") -- now forces RECONCILING except HALTED-stays-HALTED;
export_checkpoint() never emitted order.updated_at at all.
TESTS: 473 passed, 1 skipped (was 453/1); ruff clean; compileall clean.

Next: Slice 4b -- wire persistence into the pipeline's actual crash/restart flow, with the
user-specified required crash-boundary tests (before order persistence, order created but not
filled, fill received before portfolio checkpoint, after partial TP, after stop update, during
restart/reconciliation). Recovery must never duplicate an order/fill, repeat a TP tranche, lose
an open position, forget stop state, or create phantom exposure. This is the final Phase A slice.

## 2026-09-28 — PHASE A COMPLETE

TASK: Slice 4b (persistence wired into pipeline + crash/restart recovery), then the complete
Phase A gate per explicit user instruction.
MODEL: Sonnet BUILDER (direct checkout, large safety-critical spec), Sonnet 5 (Lead) thorough
review of the recovery/cross-check/orphan-handling logic + docs updates + commit.
RESULT: See commits `dadae16` (Slice 4b) and `d59840d` (docs). Single-commit-point persistence
(one transaction per pipeline call, covering all mutable state + that call's fills together);
recover_pipeline() replays fills from scratch and cross-checks against persisted rows rather
than trusting them, halting on any disagreement; orphan reservations released and audited;
missing reservations halt; execution never resumes READY on restart; a weak, explicitly-labeled
paper self-check reconcile() is the only path back to READY. Seven dedicated crash-boundary
tests prove: before-persist (clean slate), resting-unfilled-order survives restart, fill-before
-checkpoint (pre-fill state + exactly-once redrive), partial-TP persisted vs. un-persisted
(exact stage restoration vs. correct re-fire), stop-ratchet never regresses to less protective,
corrupted store halts safely and reproducibly.

COMPLETE PHASE A GATE (per explicit instruction): full suite 480 passed/1 skipped; ruff clean;
compileall clean; integration 17 passed/1 skipped; property 1 passed; recovery 7 passed; chaos
7 passed. "Liveness tests" in the sense of the later master directive (DAX/NASDAQ/WTI synthetic
scenarios) don't yet apply -- no CFD instrument model exists yet (that's Priority 2+); the
existing 15-scenario E2E suite (tests/integration/e2e_scenarios.py) already proves valid trades
execute end-to-end for the current crypto-paper system and is included in the integration count
above.

**PHASE A (realism gaps) IS NOW COMPLETE**: transaction costs (A1), exit engine incl.
multi-stage take-profit (A2), persistence/recovery (A3), margin wiring (A4), clock/latency/
health (A5), all wired into the live pipeline/risk/execution, fee-aware daily loss, restart
safety. OPEN_QUESTIONS #22, #23, #24, #25 all resolved this session. Two real pre-existing bugs
found and fixed along the way: gross_pnl cross-instrument leak (Slice 3a) and
restart-resumes-READY/missing-updated_at (Slice 4a).

Per explicit user instruction: next work moves directly to ActivTrades MT5 -> InstrumentSpec ->
CFD data ingestion -> FeatureRegistry -> OpportunityScanner -> simple Regime Engine -> Momentum/
Breakout/Pullback validation on DAX/NASDAQ/WTI -> VectorBT screening -> Nautilus replay -> OOS/
walk-forward -> ALPHA VIABILITY REPORT V1. Not further infrastructure unless a blocker is found.

## 2026-09-29 — CFD/MT5 foundation: InstrumentSpec, data ingestion, cost safety, scaffolding

TASK: Priority 1-4 credential-free foundation for the CFD/ActivTrades/MT5 expansion, per the
post-Phase-A master directive's "prepare everything possible now" mandate.
MODEL: 2x Sonnet BUILDER (parallel, independent tracks), Sonnet 5 (Lead) review + lint fixes +
commit.
WHY: Both tracks are standalone, fully fixture-testable without a live MT5 connection or
credentials, and independent of each other (data/instrument model vs. cost-schedule safety +
scaffolding) -- routed as ordinary well-specified BUILDER tasks.
RESULT: See commit `052a8cc`. `src/instruments/` (InstrumentSpec, symbol discovery/matching),
`src/data/` extensions (provenance, Parquet store, quality checks), `src/costs/` hardened
against silent cross-venue schedule reuse (new required `instrument_class`/`cost_confidence`,
`calculate_trade_costs` now raises on a mismatch), `MetaTrader5` dependency added and verified
importable on this host, `.env.example`/`PENDING_USER_INPUT.md`/`src/adapters/config.py`/four
script skeletons.
TESTS: 562 passed, 1 skipped (was 480/1); ruff clean; compileall clean.

Next: MT5 adapter itself (`src/adapters/activtrades_mt5/`), now that `InstrumentSpec` exists as
a stable contract to convert raw MT5 data into. Read-side first (typed wrappers around
initialize/account_info/symbols_get/symbol_info/copy_rates/copy_ticks/order_calc_margin/
order_calc_profit/order_check), connection/health state machine, mock MT5 client for testing
(no real terminal in this environment). Write-side interface prepared but disabled by default.

## 2026-09-29 — MT5 adapter + script wiring (credential-free foundation complete)

TASK: ActivTrades MT5 adapter (read-side + guarded write-side), then wire the four mt5_*.py
scripts to it, closing out the "prepare everything possible now" credential-free checklist.
MODEL: Sonnet BUILDER (adapter, large single task), Sonnet 5 (Lead) for script wiring (direct,
mechanical integration of already-built pieces) + verification + commits.
RESULT: See commits `0b759e9` (adapter) and `a38d6e6` (script wiring). Real MT5 field
names/signatures independently verified by the Lead (spot-checked constants via the installed
package, spot-checked struct field name strings via direct binary inspection of the installed
package's compiled extension -- both confirmed genuine, not guessed). Two items explicitly
flagged unverified pending a real terminal: sl/tp field names, filling_mode bitmask beyond
FOK/IOC (unrecognized bits recorded in quality_flags, never guessed).
Scripts now make real connection attempts and fail cleanly without a live terminal (verified:
correctly reports connection failure, never crashes, never leaks the password).
TESTS: 623 passed, 1 skipped; ruff clean; compileall clean.

**Credential-free CFD/MT5 foundation is now complete**: MetaTrader5 dependency, InstrumentSpec,
symbol discovery, data provenance/Parquet/quality, cost-schedule cross-venue safety, MT5
adapter (protocol-based, mock-testable), connection/health state machine, guarded write-side
interface, and all four operational scripts (connection check, account snapshot, symbol
discovery, preflight) wired to the real adapter. PENDING_USER_INPUT.md lists the exact,
minimal remaining steps -- MT5 demo login, four env values, one preflight command.

Remaining before Priority 3+ (FeatureRegistry/OpportunityScanner/Regime/strategy research):
live data ingestion actually calling the adapter (Parquet layer exists, unpopulated), margin/
profit comparison tests against real order_calc_margin/order_calc_profit (needs a live
connection), and everything downstream of having real CFD market data.

## 2026-09-29 — Python 3.12 restore + MT5 hang-safety + central .env config loader

TASK: Fix four reported local-environment problems: uv silently drifted to Python 3.13 (broken
3.12 managed install), MT5 connection attempts could hang, .env wasn't loaded, and (separately)
verify the real ActivTrades DEMO connection with user-provided credentials.
MODEL: Sonnet 5 (Lead), direct implementation (mechanical environment/safety fixes with a clear,
fully-specified brief -- no delegation needed).
RESULT: See commits `86bdbe5` (Python 3.12 pin + pytest pythonpath fix) and `04fb966` (config
loader + bounded MT5 IPC safety). Python reinstalled/pinned via `uv python install 3.12` + `uv
python pin 3.12`; requires-python narrowed to `<3.13`. New central `load_mt5_connection_config()`
.env support (process env still wins), redacting `__repr__`. New
`src/adapters/activtrades_mt5/bounded.py` (subprocess+timeout, kills a hung child, never the
terminal), `diagnostics.py` (12 precise categories from real MT5 `RES_E_*` codes), `probe.py`
(testable isolated probe). All four scripts + new `mt5_ipc_probe.py` now self-reinvoke via the
bounded pattern. `mt5_preflight.py` restructured to print each stage incrementally so a later
hang doesn't lose earlier results. New `run_mt5_preflight.ps1`/`.cmd` double-click launchers.
Real bug found+fixed along the way: a killed child's stdout was lost without `-u` (unbuffered).
TESTS: 651 passed, 1 skipped (was 623/1); ruff clean; compileall clean (all via `uv run`).

Real-world finding: with real ActivTrades DEMO credentials configured locally (`.env`, never
committed/printed -- verified absent from every tracked file), the bounded preflight correctly
loads config and cleanly reports MT5_IPC_TIMEOUT after the configured bound rather than hanging
(confirmed zero orphaned processes afterward). The underlying connection still fails from this
automation session specifically; consistent with this session's own earlier diagnosis (shell
processes here likely lack interactive-desktop access to the MT5 terminal GUI -- a Windows
session/window-station limitation, not a code or config bug). The user needs to run
`run_mt5_preflight.ps1` (or `uv run python scripts/mt5_preflight.py`) directly on their own
desktop to get a real result.

## 2026-09-29 — MT5 connection troubleshooting resolved: live and verified

TASK: Diagnose and resolve the persistent MT5_IPC_TIMEOUT that blocked all real-connection
verification. Extensive read-only environment diagnostics (process/user/integrity/bitness/
version/multi-instance checks, isolated portable-instance A/B test, attach-vs-login separation)
all ruled out code/config causes; a build-regression hypothesis could not be tested responsibly
(no trusted archive of old MT5 builds exists) and was explicitly not assumed.
RESULT: Root cause was environmental/terminal-settings-related (most likely the "Allow
algorithmic trading" toggle in the terminal's own Expert Advisors options, per public MQL5
forum evidence gathered during research) -- resolved on the user's own desktop, not via a code
change. Confirmed end-to-end, user-run: config/terminal_connection/account_state/order_check
all PASS against the real ActivTrades demo account (EUR 500 equity). This session's own
automation-shell processes still cannot reliably reach the MT5 terminal's IPC (a separate,
accepted limitation -- see `run_mt5_preflight.ps1`/`.cmd`), but the adapter and real broker
connection are now confirmed genuinely working.

Real bug found and fixed along the way, from live evidence (`scripts/mt5_diag_order_check_raw.py`,
never sends a real order): `order_check()` uses `retcode=0` for success, NOT `order_send()`'s
`TRADE_RETCODE_DONE=10009` convention -- a genuinely valid, fundable request was being
misclassified as FAIL. `_ORDER_CHECK_SUCCESS_RETCODES` now includes 0 as the primary, verified
value; regression test added; test fixture default corrected.

Also fixed a real test-design flaw the now-working environment exposed:
`tests/unit/scripts/test_mt5_scripts.py`'s "fake credentials always fail cleanly" test assumed
a real connection attempt against wrong credentials could never succeed -- but MT5's
`initialize()` can attach to an already-running, already-authenticated terminal session
regardless of mismatched login/password/server, which is real MT5 behavior outside this
codebase's control. Test now asserts what actually matters (clean exit, config genuinely
loaded, password never leaked) instead of assuming a specific outcome; verified stable across
multiple real runs against the live terminal.

WTI confirmed by the user as genuinely unavailable on this demo account (not a matching bug) --
kept in the target instrument set for later real-account trading per explicit instruction; the
symbol-matching code is fully generic and needs no change for that.

TESTS: 652 passed, 1 skipped; ruff clean; compileall clean.

**Milestone: the ActivTrades MT5 adapter is now a confirmed-working, live-verified connection
to a real demo account**, not just a tested-in-isolation module. DAX and NASDAQ100 are
confirmed tradeable (order_check PASS on DAX). Next: live/historical data ingestion actually
calling the adapter (per PENDING_USER_INPUT.md), then FeatureRegistry/OpportunityScanner/Regime
Engine and the DAX/NASDAQ research vertical slice, per the master directive's priority order.

## Critical fix: account-protection invariant (2026-09-29)

TASK: the user reported their manually logged-in ActivTrades demo account was repeatedly being
logged out while this repo's own development activity (running the test suite) ran, and had to
be re-authenticated by hand every time. This is unacceptable for a safety-critical repo and was
treated as a stop-everything fix.

ROOT CAUSE (confirmed): `MT5Connection.connect()` and `probe.run_isolated_probe()`
unconditionally called `MetaTrader5.initialize(path, login=, password=, server=)` -- real
authentication -- for every real MT5 access, including read-only diagnostics. Worse,
`tests/unit/scripts/test_mt5_scripts.py` had a test
(`test_script_attempts_real_connection_without_crashing_or_leaking_password`) that ran four
scripts' real subprocess workers with synthetic fake credentials as part of ordinary `pytest`.
MT5 has exactly one current account per terminal instance, so this real, credentialed
`initialize()` call against the SAME real, already-authenticated terminal -- happening every
time `pytest` ran -- is what was switching/disconnecting the user's manually logged-in session.

FIX:
- `MT5Connection.connect()`/`probe.run_isolated_probe()` now default to a credential-free
  `initialize(terminal_path)` (attach-only, exactly like a second manual terminal instance would
  do), verify the already-attached account's `login` against the configured one, and fail closed
  (`ACCOUNT_MISMATCH`) on any mismatch -- never logging in or switching the account automatically.
- Real authentication only happens when `config.allow_account_login` is explicitly `True`, set
  via `MT5_ALLOW_ACCOUNT_LOGIN=1` read ONLY from the process environment (never `.env`, so it can
  never become "sticky"). No test/preflight/discovery/downloader/ruff/compileall enables it.
- A single-owner file lock (`adapters/activtrades_mt5/lock.py`) now gates every real
  `initialize()`/`login()` call site in the repo (including the two temporary diagnostic scripts
  that still perform real IPC) -- a second concurrent real-MT5 access gets `CONNECTION_BUSY`
  immediately instead of racing. Lock staleness window tuned to 90s (was tried at 300s first,
  then tightened after discovering a killed/timed-out worker leaks the lock past its own
  `finally` block -- 90s clears a leaked lock well before it would block a legitimate retry).
  `MT5Connection`/`run_isolated_probe` take an injectable `lock_path` so tests never share the
  real, process-wide lock file.
- The dangerous pytest test above was replaced: `run_worker_bounded` is now faked in every
  script test, so ordinary `pytest` can no longer reach real MT5 in any way, not just "not with
  real credentials" -- a strictly stronger guarantee, and structurally impossible to regress
  silently (any future script that bypasses the fake would raise, not silently connect for real).
- Codex adversarial review (`codex-companion.mjs adversarial-review`) caught two real MT5 call
  sites this pass initially missed: `mt5_diag_attach_then_login.py`'s Test 1 attach and
  `mt5_diag_portable_instance.py`'s attach, both real `initialize()` calls outside the lock. Both
  fixed to acquire the lock before their first real call.

TESTS: 677 passed, 1 skipped; ruff clean; compileall clean. New coverage:
`tests/unit/adapters/activtrades_mt5/test_account_protection.py` (8 tests: no-credentials
default, account-mismatch fail-closed, unreadable-account fail-closed, explicit-auth path,
lock busy/release semantics), `tests/unit/adapters/activtrades_mt5/test_lock.py` (6 tests: the
lock primitive itself), plus updated `test_config.py`/`test_probe.py`/`test_connection.py`/
`test_mt5_scripts.py`.

VERIFICATION LIMIT: this session's own automation-shell processes still cannot reliably reach
the real MT5 terminal's IPC (the same pre-existing, accepted limitation noted elsewhere in this
ledger) -- one attempt to verify live account identity before/after the gate timed out cleanly
(terminal left untouched, exactly as designed) rather than confirming a live before/after
account match. The user should independently confirm on their own desktop, once, that running
`uv run pytest` no longer logs their terminal out.

## Architecture ownership audit (2026-09-29) — report only, no convergence implemented

TASK: pre-alpha ownership audit (Nautilus vs custom stack). Report: `docs/ARCHITECTURE_AUDIT_2026-09-29.md`.
MODELS: lead Sonnet (git/baseline/synthesis, direct); Haiku scout (module inventory; research/ claim wrong, discarded);
Sonnet general-purpose (MT5 adapter + mt5-connector benchmark); Codex read-only adversarial review (task-mumgymub-ysr3nw). Opus not needed.
RESULT: safety fix committed `ba93000`; baseline branch `architecture/nautilus-convergence-baseline` + tag `baseline-green-2026-09-29` pushed.
FINDING: `src/` has zero nautilus_trader imports; custom stack duplicates orders/positions/PnL/reservations/halt/reconciliation state.
NEXT DECISION: user review of the audit before any convergence step (C1 first).

## Nautilus convergence C1 + C2 (2026-09-29) — uncommitted, awaiting review

TASK: C1 engine-agnostic safety contracts, C2 pure RiskPolicy/PositionSizer split, plus two confirmed semantic bug fixes.
MODELS: lead Sonnet (C2, both bug fixes, their tests, docs — direct); Sonnet builder (tests/contracts, 183 tests, tests-only);
Codex read-only adversarial review (3 High + 1 Medium found, all fixed; re-verified by full suite).
BUG 1: `realized_pnl_today` was all-time realized PnL minus all-time fees -> now trading-day windowed (`risk/trading_day.py`, default UTC midnight, PENDING broker calibration).
BUG 2: READY was treated as reconciled, `last_reconciled_at=now` fabricated, recovery "reconciled" against itself -> `ReconciliationState` owned by the engine, set only by `reconcile()`.
RESULT: 1299 passed, 1 skipped; ruff + compileall clean. Old engines kept, marked LEGACY_RUNTIME / SHADOW_ORACLE.
NEXT DECISION: user review, then commit; C3 (GER40 data plane) only after.

## C2.1 final reconciliation hardening (2026-09-29)

TASK: persist reconciliation source; reduce-only defense in depth at execution admission; outbound-vs-inbound invariant.
MODELS: lead Sonnet (direct); Codex read-only review x2 (1 Critical + 3 High, then 1 High; all fixed or decided, see below).
DECISION: simulated fills of RESTING reduce-only/protective orders stay ungated (paper stand-in for broker-side stops); documented in docs/EXECUTION_CONTRACT.md.
RESULT: 1344 passed, 1 skipped; ruff + compileall clean. DST/rollover still PENDING_BROKER_CALIBRATION. C3 not started.

## C3 GER40 historical data plane (2026-09-29)

TASK: MT5 historical read path -> normalization -> validation -> provenance -> deterministic Parquet, against REAL ActivTrades (DEMO) schema.
MODELS: lead Sonnet, direct (small, sequential; delegation overhead not worthwhile). Codex review deferred to the single post-C4 review.
BROKER FACTS (observed live, attach-only, read-only, demo account trade_mode=0, terminal build 6231):
- Broker symbol for canonical GER40 = `Ger40` (path `Cash Indices\Ger40`, "DAX Cash Index"). Also present: `Ger40Dec26` (dated), `GerMid50`, `GerTec`. `Ger40Dec26` deliberately NOT mapped.
- copy_rates_* dtype: time <i8 (epoch s, bar OPEN), open/high/low/close <f8, tick_volume <u8, spread <i4 (POINTS), real_volume <u8 (always 0).
- copy_ticks_* dtype: time <i8, bid/ask/last <f8, volume <u8, time_msc <i8, flags <u4, volume_real <f8. last=0.0, volume=0 always (CFD index) -> last stored as null.
- symbol_info: digits 2, point 0.01, trade_tick_size 0.01, tick_value 0.01, contract_size 1.0, volume_min 0.25 / step 0.25 / max 250, currency EUR (base/profit/margin), trade_calc_mode 4, trade_mode 4, exemode 2 (MARKET), filling_mode 3, stops_level 100, margin_initial/maintenance 0.0 (not provided), swap_long -5.445 / short -0.555, margin_hedged 0.2. Snapshot: tests/fixtures/ger40/symbol_info.json.
- TIME: MT5 `time` is SERVER wall clock encoded as epoch, NOT UTC. Measured tick time vs system UTC = +7199 s (2026-09-29, CEST). Policy: Europe/Berlin (`ServerTimePolicy`), basis INFERRED, not broker-confirmed. Ambiguous/non-existent DST-hour values fail closed. Range requests must be passed in server epoch (`utc_to_request_datetime`); a true-UTC end silently clipped 2h of data.
- Session structure (server clock): daily last M5 bar 21:55; first bar 02:15 (summer) / 01:15 (winter) in most sampled weeks = 00:15 UTC. ANOMALY (unresolved): week of 2025-11-03 started 02:15 (=01:15 UTC) while other winter weeks start 01:15. User note: may be a DEMO-account limitation, not a rule of the live account -> do not generalize; re-check on live/other data before relying on the session calendar. 2026-10-12 week returned no data (future/unavailable).
- Sample week 2026-09-21..25: M1 5925 bars, M5 1185 bars (= 5 x 237, 00:15-19:55 UTC), zero missing intraday bars, 4 expected session gaps, 0 suspicious; 3441 ticks (2026-09-25 08:00-08:10 UTC) clean. Typical bar spread 155-570 points (1.55-5.7 index points).
DECISIONS: parquet is single-file-per-range with provenance in Parquet metadata + content_sha256 (verified on read); FAILED validation is never persisted; Decimal columns stored as exact strings (existing convention); `BarRecord.spread_points` added (optional). Old day-partition `ParquetStore` untouched.
ASSUMPTIONS / DEFERRED: DST offset rule (Europe/Berlin) needs broker confirmation; holidays surface as SUSPICIOUS gaps (reviewed, not hidden); trading-day rollover still PENDING_BROKER_CALIBRATION; no M1 fixture committed (M5 week only); real data lives in git-ignored data/c3_sample, offline tests use tests/fixtures/ger40.
FAILED/REJECTED: an earlier session's history.py docstring claimed live verification but had no tests or time handling (treated server epoch as UTC) -> rewritten and re-verified this session.

## C4 real Nautilus 1.231.0 GER40 backtest (2026-09-29)

TASK: prove the Nautilus lifecycle (instrument -> catalog -> BacktestEngine -> Strategy -> RiskPolicy/PositionSizer -> order -> fill -> position -> portfolio -> realized PnL) from the validated C3 dataset. Labels: TECHNICAL_BACKTEST / NOT_YET_BROKER_CALIBRATED.
MODELS: lead Sonnet direct; Codex focused read-only review after C4 (see below).
LAYOUT: `src/nautilus_kernel/` (instrument, catalog, risk_bridge, proof_strategy, backtest, queries, proof) + `tests/unit/nautilus_kernel/` + `scripts/c4_run_proof.py`. No competing order/position/portfolio state (import-guard test).
DECISIONS:
- Instrument: `nautilus_trader.model.instruments.Cfd`, id `GER40.ACTIVTRADES`, raw_symbol `Ger40`, asset class INDEX, EUR, price precision 2 / tick 0.01, size step/min 0.25, max 250 -- all from the real symbol_info snapshot. Assumptions (explicit in `INSTRUMENT_ASSUMPTIONS`): margin_init/maint 5% (broker gave 0.0), fees 0, swap not modelled, contract size must be 1.0. Demo account facts: leverage 30, EUR, margin_mode 0 (retail NETTING) -> engine uses OmsType.NETTING, StandardMarginModel.
- Bars: MT5 BID OHLC as `...-5-MINUTE-BID-EXTERNAL`; ASK bar SYNTHESIZED = bid + bar `spread` points (constant per bar); bars stamped at CLOSE (open+tf). Volume = broker tick activity.
- FAILED APPROACH / FINDING: without a latency model Nautilus filled the strategy's market order against the PREVIOUS bar's book (fill = prev bar close -/+ 1 tick): a price older than the decision, look-ahead-favourable for momentum. Fixed with `LatencyModel(1s)` -> fills at decision-bar close on the executable side (BUY ask, SELL bid) + 1 tick slippage; mutation test proves the test catches the regression.
- FINDING: with NETTING, Nautilus moves every completed lifecycle into position SNAPSHOTS; `cache.positions_closed()` shows only the latest. First run therefore under-counted trades and consecutive losses (gate silently inert). Fixed via `queries.closed_position_lifecycles` (snapshots + closed).
- Risk bridge: reads equity/unrealized/exposure/closed-trade history from Nautilus portfolio+cache each decision; only bridge-local derived value = peak_equity. RuntimeRiskState fixed READY; pending exposure empty (v1 = one order in flight). RECONCILED asserted ONLY because Nautilus is the sole ledger in a backtest -- must not be reused for live/demo (C5: VENUE_SNAPSHOT). Trading-day policy unchanged: UTC midnight, PENDING_BROKER_CALIBRATION.
- Proof strategy: EMA(8)/EMA(21) cross on closed M5 BID bars, stop = 2*ATR(14) as a reduce-only Nautilus STOP_MARKET, exits: opposite signal (flatten first, reversal only on a later bar), 24-bar time exit, session end 19:30 UTC. One position max; same-direction signal ignored.
- Sample result (real week 2026-09-21..25, demo data): 1185 bars, ~18 trades, small net PnL, loss-streak gate engaged -- NOT a profitability claim.
ASSUMPTIONS / LIMITS: fills at decision-bar close (+1s latency, +1 tick), not tick-perfect; synthesized ask; no swap/commission; liquidity assumed 1,000,000 EUR notional; broker cost calibration incomplete; the Nov-2025 session-start anomaly from C3 (possibly demo-specific) untouched.
DEFERRED: real ExitEngine, FeatureRegistry/regime, C5 Nautilus MT5 InstrumentProvider/DataClient/ExecutionClient, DST/rollover calibration, tick-level execution model, Pandas4Warning noise from Nautilus internals.

### C4 Codex review (post-C4, read-only, one pass) and fixes (2026-09-29)
FIXED (High): (1) new C3/C4 MT5 scripts could authenticate/switch account if MT5_ALLOW_ACCOUNT_LOGIN=1 was inherited -> `adapters.config.load_attach_only_config` refuses it before any client is obtained; new test covers all 5 scripts. (2) C4 consumed datasets with PASSED_WITH_WARNINGS -> `run_proof` now requires `validation_status == PASSED` unless `accept_warnings=True`. (3) Fills at the decision bar's own close were look-ahead-favourable -> execution model changed to EXECUTION_DELAYED_ONE_BAR (latency = 1 bar + 1 ns => priced at the LATER bar's close on the executable side, +1 tick); pessimistic, not next-open (bars carry one timestamp; Nautilus cannot fill at a bar open). This supersedes the earlier "1 s latency" entry above; the mutation test still proves the stale-book/decision-close regressions are caught. Consequence: the protective stop and exits also arrive one bar late (documented, conservative).
FIXED (Medium): diagnostics now shift range requests to server epoch; rates/ticks schema check validates full dtypes, not only names.
NOT FIXED / DEFERRED (Medium): (a) `RECONCILED`/`DataQuality.LIVE`/READY/assumed liquidity are asserted inside the backtest risk bridge -- acceptable only because Nautilus is the sole ledger in simulation; C5 must feed real VENUE_SNAPSHOT reconciliation and live data quality, and a production-mode rejection test for synthetic inputs is still owed. (b) `peak_equity` lives in the bridge instance (drawdown high-water mark resets if the bridge is recreated mid-run); derive from Nautilus account history in a later phase. 
Codex did not report on volume semantics, lifecycle ownership, API correctness, instrument assumptions, dataset determinism, C3/C4 separation (unclear whether clean or uncovered); those are covered by our own tests (import-guard, byte-identical catalog/parquet).

## C5 Nautilus <-> ActivTrades MT5 adapter (2026-09-29)

BASE: 65c71ea (C3+C4 complete). Package `src/nautilus_mt5/`; tests `tests/unit/nautilus_mt5/`. No real MT5, no demo order in C5.
### C5A - foundation (provider, symbols, retcodes, restart-safe state, fake broker)
- `Mt5InstrumentProvider` builds the Nautilus `Cfd` from live `symbol_info`; anything MT5 does not report (margin rates, fees) comes from an explicit `InstrumentAssumptions` and is tagged `ASSUMED` in `Cfd.info` -- C4 assumptions are NOT inherited. `SymbolRegistry` (canonical GER40 <-> broker Ger40); NASDAQ100 is deliberately not registered until its real symbol is observed.
- `constants.py`: MT5 constants verified against the installed package; `classify_send_retcode`: order_check success is retcode 0 ("Done"), order_send success is DONE/DONE_PARTIAL/PLACED; TIMEOUT/CONNECTION/ERROR/unknown codes are IN_DOUBT (never assumed rejected). The legacy `order_check_result_from_mt5` still also accepts 10008/10009 (defensive extras, existing tests); the adapter does NOT use it -- it checks `retcode == 0` itself.
- `state.Mt5StateStore` (SQLite): write-ahead intent with a token carried in the MT5 order comment, ClientOrderId <-> order/position ticket map, ingested-deal set. Identity tables only.
- `adapters/activtrades_mt5/fake_broker.py`: stateful NETTING broker double (order_check/order_send semantics, attached SL/TP executed broker-side with DEAL_REASON_SL/TP, progressive/hidden/duplicate deals, lost responses, disconnects, external fills).
- Concrete defect fixed (not a reopen of C1-C4 scope): `MT5Connection.check_ready` compared MT5 `tick.time` (SERVER clock epoch) against UTC, understating quote age by the server offset (~2 h). New optional `server_time_policy` argument corrects it; default unchanged for compatibility, the adapter always passes it. `DealRecord` gained `reason`/`time_msc`; `MT5ClientProtocol` gained `copy_rates_from_pos`.
### C5B - session + data client
- `Mt5Session`: ATTACH ONLY (a config with `allow_account_login=True` is refused at construction), wraps `MT5Connection` (expected-account verification, single-owner lock, stale-lock recovery). Deterministic states DISCONNECTED/CONNECTED/DEGRADED/FAILED; `generation` bumps on every (re)connect so consumers know reconciliation is required; a lost terminal degrades the session; account mismatch / lock busy => FAILED (no retry, no login). `account_mode()` detects margin mode: RETAIL_NETTING supported (observed ActivTrades demo), HEDGING/EXCHANGE/unknown fail closed.
- `Mt5LiveMarketDataClient`: quotes from `symbol_info_tick` (dedupe by time_msc/bid/ask, sizes published as 0 = unknown depth), completed BID bars only (stamped at close, first poll is baseline only), server->UTC via `ServerTimePolicy` (INFERRED, not broker-confirmed; ambiguous DST hours dropped and counted), invalid/crossed quotes never published. ONE controlled poller task (cancelled on last unsubscribe/disconnect, bounded back-off, attach-only reconnect); `poll_once()` is the deterministic unit under test. Base `InstrumentProvider.initialize()` loads nothing without config, so the client calls `load_all_async()` explicitly.
- Tooling lesson: `ruff format src` reformatted unrelated files; restored with `git checkout`. Format only explicit paths.
### C5C - execution client, reconciliation, dedupe, protection (tests: real Nautilus ExecutionEngine + Cache + Portfolio over the stateful fake broker)
- `Mt5LiveExecutionClient` (NETTING only; hedging/exchange/unknown margin modes fail closed at connect). Nautilus owns everything trading-related; adapter state = `Mt5StateStore` (ClientOrderId<->order/position ticket map incl. bracket parent->child links, ingested-deal set) + `ReconciliationTracker`.
- IDs: write-ahead intent row + token in the MT5 `comment` (NT<n>, <=31 chars) BEFORE `order_send`; restart re-associates broker orders/deals by ticket or token. Fill identity = MT5 deal ticket (`TradeId`); venue order id = MT5 order ticket; protective orders keep the venue id `SL:<position ticket>` they were accepted with (Nautilus rejects a fill with a different venue id -- found by test). NETTING: Nautilus assigns position ids, MT5 position ticket is kept only in the map.
- Dedupe: `_ingest_deal` books the fill in Nautilus first, marks the deal seen only afterwards; a failure while booking leaves it unmarked (retried, never lost); booking follow-ups (account state, protective-order retirement) are best-effort and idempotent so they can never cause a double fill.
- Outbound gates (`gates.py`, C2.1 semantics): NEW_EXPOSURE needs READY + RECONCILED(VENUE_SNAPSHOT) + no unprotected position; REDUCE_ONLY needs RECONCILED(VENUE_SNAPSHOT) and READY/HALTED; protection tighten needs broker-verified ticket only; loosen/remove needs READY+RECONCILED. Second admission at the last step from FRESH broker evidence (position exists, side, qty<=position with NO clamping, local==broker). Inbound (deals, SL/TP executions, external trades) never consults the gates -- tested while HALTED/MISMATCH.
- Reconciliation: connect/reconnect/restart => NOT_RECONCILED; `reconcile()` ingests deals, settles in-doubt orders (adopt by token, or reject only after a grace period with no broker trace), compares positions/working orders with the Nautilus cache; only a clean comparison grants RECONCILED(VENUE_SNAPSHOT); any discrepancy => MISMATCH + HALTED until a later clean comparison. `PAPER_SELF_CHECK` cannot reconcile a broker account (raises).
- order_send outcomes: REJECTED (definite) vs IN_DOUBT (timeout/None/exception/unknown retcode => no rejection event, exposure assumed possible, reconciliation invalidated). order_check success is retcode 0; order_send success is DONE/DONE_PARTIAL/PLACED.
- Protection design (answer to the C7 race): entries carry SL/TP in the SAME order_send (broker-side from the first instant); a bare market entry is denied when `require_attached_protection`. Nautilus STOP_MARKET/LIMIT reduce-only orders map to MT5 POSITION SL/TP (not pending orders: in a netting account a pending stop could open a reverse position). If the entry filled remotely while local reconciliation was lost, `emergency_protect(ticket, sl)` is tighten-only, needs fresh broker evidence of the ticket, works in any reconciliation state. SL/TP changes: tighten OK anywhere, loosen/remove need RECONCILED. Bracket children are write-ahead rows; promoted when the parent exists at the broker (also after in-doubt adoption), denied when the parent is denied/rejected.
- Broker truth captured: commission (negated to Nautilus cost sign), deal profit/swap/reason in `info` (audit only, Nautilus books PnL itself), account balance/margin from `account_info` (`free = balance - margin` keeps Nautilus' invariant; raw broker margin_free in `info`). C4's 0-commission / 5%-margin assumptions are never used as broker truth (instrument margin/fees are tagged ASSUMED).
- Tooling findings: Nautilus logging is silent unless initialised, so a rejected event looks like a no-op -- debug by inspecting `order.events`. `LiveExecutionEngine.execute` only queues (needs `start()`); tests use the plain `ExecutionEngine` and the live one only for Nautilus' own mass-status/report reconciliation. ClientId must equal the account-id issuer (`ACTIVTRADES`).
- Deferred: MT5 calls run synchronously on the event loop (bounded by MT5 IPC timeouts; move to a single-thread executor before live use); pending (limit/stop entry) orders unsupported (denied); modify only for protective orders; TradingNode config/factory classes (C7); real order_check/SLTP acceptance on the actual ActivTrades terminal is UNVERIFIED (no order-related terminal call was made in C5); DST/rollover unchanged.
### C5 Codex review (one read-only pass) and fixes
FIXED (High): (1) crash between `generate_order_filled` and `mark_ingested`: replay now skips publication when Nautilus already holds the trade id (order.trade_ids), and a cold cache (restart) books the fill through reports (client_order_id attached; Nautilus dedupes by trade id); (2) delayed DONE_PARTIAL left IOC orders PARTIALLY_FILLED: remainder is now finalized from BROKER order state (CANCELED/REJECTED/EXPIRED) after every deal ingestion; (3) `emergency_protect` reported success on broker rejection/unverified result: now returns `ORDER_SEND_<retcode>` / `PROTECTIVE_OUTCOME_UNKNOWN`; (4) reduce-only race: position re-read immediately before `order_send` (side + volume), and post-send verification that exposure did not increase (else MISMATCH); (5) restart fill reports used `deal.order` instead of the accepted venue id (`SL:<ticket>`): reports now use the persisted row's venue id.
FIXED (Medium): cumulative `failed` counter blocked RECONCILED forever (now the set of currently failing deal tickets); in-doubt adoption now persists the venue id and emits OrderAccepted; protective cancel is only reported after the level is verified gone; `Mt5Session.acquire` no longer inflates the refcount on failed connects.
NOT FIXED / DEFERRED: per-symbol serialization of venue mutations is implicit (single event loop, synchronous MT5 calls) -- an explicit lock/executor is needed once MT5 calls move off-loop; Codex reported nothing for areas 2, 3 (beyond refcount), 11, 13, 14, 15 (unclear whether clean or not covered; covered by our tests: zero-real-MT5 autouse guard, attach-only session, netting/hedging detection, order_check/order_send/IN_DOUBT table, filling/deviation tests). No second Codex confirmation pass was run; fixes are pinned by regression tests (`test_c5c_review_fixes.py`).

## C6 shadow / semantic parity + 30x ceiling (2026-09-29)
BASE 16aabff. Scope: narrow parity of economic/safety semantics between the LEGACY ORACLE (PaperExecutionEngine + Portfolio + RiskEngine) and the NAUTILUS path (real Nautilus ExecutionEngine/Cache/Portfolio behind the C5 MT5 execution client over the stateful fake broker; risk via NautilusRiskBridge -> RiskPolicyEvaluator). Legacy is not authority: every divergence was classified against the documented contract (docs/EXECUTION_CONTRACT.md, RISK_CONTRACT.md).
LEVERAGE CEILING: `risk.models.MAX_SYSTEM_LEVERAGE` 20 -> 30 (ActivTrades allows 1:30). It is a CEILING, never a default or target: sizing is still risk-budget/stop/exposure driven (parity test: median approved leverage < 10x; a policy/instrument above 30 cannot be constructed). Updated contracts (parametrized at 20x and 30x), unit/property/integration tests, docs (ARCHITECTURE, RISK_CONTRACT, TEST_GATES, README). C4's technical policy (20x) is a configured cap below the ceiling and unchanged; the bridge's account leverage cap now defaults to the ceiling (broker account leverage 30 observed on the demo).
RESULTS (artifacts/parity/execution_parity.json, deterministic; risk grid = 3 policy caps x 3 balances x 2 unrealized x 3 loss streaks x 2 sides x 3 stops x 2 spreads = 648 cases, legacy RiskEngine vs bridge path fed IDENTICAL inputs: 0 disagreements):
- MATCH: full fill, partial-fill accumulation, duplicate-fill booked once, partial close 1.00->0.75 (+realized PnL), full close PnL, short round trip, new-exposure admission matrix and reduce-only admission matrix (9 runtime x reconciliation states each), inbound fill while HALTED/MISMATCH, reconciliation mismatch -> halt -> clean re-reconcile clears it, risk decisions/sizes/reference price/stop validation/30x cap.
- EXPECTED_DIFFERENCE: fee source (legacy estimates from a cost schedule, Nautilus books broker-reported commission); one-position/no-pyramiding and no-same-tick-flip are enforced by the adapter's final admission while legacy execution leaves them to the layer above (oversized reduce-only is blocked by both); bare entry without a stop is refused by the adapter (C6/C7 hard gate); restart with an open position (legacy restores from its checkpoint, Nautilus cache is rebuilt by Nautilus mass-status reconciliation, adapter reports MISMATCH until then); protective stop representation (legacy child order vs broker-side position SL) with identical lifecycle semantics.
- LEGACY_BUG (Medium, recorded, not patched: retired oracle): a fill for an UNKNOWN order halts and DROPS the fill, contradicting the contract 'never drops the event'. Nautilus/adapter books it as reported and halts.
- NAUTILUS/ADAPTER_BUG: none. UNRESOLVED: none.

## C6.5 MT5 I/O hardening: dedicated execution lane + serialization (2026-09-29)
PROBLEM (C5 deferred): MT5 IPC was synchronous on/adjacent to the event loop. FIX: `nautilus_mt5/executor.py`.
- `Mt5Executor`: ONE dedicated worker thread, FIFO; `run()` (awaitable) / `run_sync()`; `assert_in_lane`; stats (max_concurrent must be 1). `Mt5Session(lane=...)` REFUSES every MT5 call (connect/disconnect/reconnect/`call`/any attribute of `session.client`, which becomes a guard proxy) made from another thread (`LaneViolation`), so nothing can bypass the lane. The global single-owner terminal lock and attach-only rules are unchanged.
- Nautilus objects are only touched on the loop thread: `LoopBridge` marshals every generate_*/report emission and every cache access made from lane code back to the loop (`_MarshalledCache`, wrapped emitters); on the loop thread it is a direct call, so single-threaded unit tests are unchanged. Async handlers (`_submit_order`, `_submit_order_list`, `_modify_order`, `_cancel_*`, report generators, pollers, connect/disconnect) run their synchronous cores on the lane (`lane_op` / `exposure_op`); `reconcile_async` added. `Mt5StateStore` is now thread-safe (one locked connection). `build_mt5_adapter(use_lane=True)` is the default wiring.
- Per-symbol serialization: exposure-changing cores (submit, modify, cancel, protect, reduce, reconcile over all registered symbols) execute inside `lane.symbol_section(symbol)` (re-entrant lock) in addition to the FIFO lane. Tests: 30 sequential + 6-thread contention with peak concurrency 1, strict FIFO, no interleaving inside a symbol section, three concurrent bracket entries + reconcile + sync => exactly ONE order_send reached the broker, one position, DENIED x2.
- TIMEOUT != REJECTED: `LaneTimeout` = 'caller stopped waiting', never a verdict. On expiry of an exposure-changing operation the involved (parent) orders become IN_DOUBT, reconciliation authority is dropped, the session is degraded, NO rejection event is emitted and nothing is resent; the still-running lane call fails closed at its next MT5 step (session degraded) and only an attach-only reconnect + explicit venue comparison restores RECONCILED (test with a 0.6 s order_send and 0.15 s patience: exactly 1 send, position 0.25, order later FILLED via reconcile, never REJECTED).
- Findings: bridge children were marked IN_DOUBT independently of their parent (blocked reconciliation) -> fixed (children follow the parent; promotion also from IN_DOUBT). A test double that counted nested same-thread calls (order_check -> account_info) as concurrency was corrected to count distinct threads. Environment note: the workstation ran near 0.4 GB free RAM; numpy/OpenBLAS failed to allocate -> set OPENBLAS_NUM_THREADS=1 for test runs.
- Residual: lane code reads mutable Nautilus order fields (status, filled_qty) from the lane thread (mutations only happen on the loop thread); these reads are advisory and the final defenses (fresh broker evidence, reconcile) do not depend on them.

## C6.5b real-terminal preflight findings (2026-09-29, ActivTrades DEMO, attach-only, read-only + order_check)
FACTS OBSERVED (artifacts/c7/preflight.json): account is DEMO (trade_mode 0), EUR, RETAIL_NETTING (margin_mode 0), leverage 30, balance/equity 500.00; terminal build 6231, trading allowed; Ger40: min lot 0.25 / step 0.25 / max 250, digits 2, stop level 1.00, filling mask 3 (FOK|IOC), execution mode MARKET, calc mode 4; book flat; spread 1.7-2.1 points; margin for a min-lot position ~319 EUR (0.25 x ~25540 x 5%, i.e. the effective index margin rate is 1:20 although the account leverage is 30).
`order_check` on the EXACT adapter request shapes (min-lot BUY/SELL market entry with attached SL; BUY with SL+TP; IOC filling; deviation 20; comment token) => retcode 0 / comment 'Done' for all three; negative control (SL above bid) => 10016 'Invalid stops', so the check discriminates. SL/TP MODIFY and CLOSE shapes cannot be order_check'ed without an open position; the adapter runs order_check on every such request at run time before order_send.
REAL-API DEFECTS FOUND (invisible to the fakes) AND FIXED: (1) forwarding an EMPTY **kwargs to a MetaTrader5 builtin (`fn(*args, **{})`) makes it reject positional arguments ('Unnamed arguments not allowed'): `Mt5Session.call` and the lane guard proxy did exactly that => every positional call (order_check(request), symbol_info(name), ...) would have failed on the real terminal; both now branch on `kwargs`. (2) `positions_get`/`orders_get` accept KEYWORD filters only (`positions_get("Ger40")`, `positions_get(None, ticket=..)` => None): adapter now uses `symbol=`/`ticket=`. (3) `history_orders_get(None, None)` => 'Invalid arguments' (needs a date range or ticket=/position=); an unknown ticket returns None, not (): adapter uses a date window / keyword ticket and treats None as 'not found'. The fake broker now reproduces (2) and (3); an AST test pins (1). Lesson: a fake that is more permissive than the real API hides call-form defects -- real order_check preflight is mandatory before any order.

## C7 first ActivTrades DEMO vertical slice -- wiring, dry run, restart mechanics (2026-09-29)
STATUS: prepared and verified up to (excluding) `order_send`. The actual demo order is a broker trade: it is NOT executed by the AI session (policy: no trade execution by the assistant, even on a demo account); the user runs it deliberately with an explicit confirmation flag.
- `nautilus_mt5/demo_slice.py`: assembles REAL Nautilus components (MessageBus, Cache, Portfolio, DataEngine, RiskEngine, ExecutionEngine, Trader) around the lane-backed MT5 clients; `DemoProofStrategy` is a one-shot state machine (no alpha): first fresh quote -> `NautilusRiskBridge` (RiskPolicy + PositionSizer, explicit demo-proof policy: risk 3%, gross/net cap 8000 EUR => the exposure caps, not the 30x ceiling, make the approved quantity the 0.25 minimum lot) -> bracket (MARKET IOC + reduce-only STOP_MARKET, stop = bid - 40 points) -> broker fill -> Nautilus position -> hold 8 s -> `close_position` (reduce-only) -> PnL -> fresh reconciliation. STOP conditions: non-demo account, non-netting, book not flat, adapter not RECONCILED, stale quote / wide spread (no entry), risk rejection, quantity != minimum lot; no retry of any exposure-changing request.
- `scripts/c7_preflight.py` (read-only + order_check), `scripts/c7_demo_vertical_slice.py` with modes `--dry-run` (default; real quotes + real order_check, order_send NEVER called), `--live --confirm-demo-order=I-AUTHORIZE-ONE-MIN-LOT-GER40-DEMO-TRADE`, `--restart-proof`. Reports: artifacts/c7/{preflight,dry_run_report,demo_slice_report,restart_proof}.json (no credentials). A leftover open position is reported for MANUAL close (reduce-only is deliberately gated while unreconciled).
- DRY RUN on the real demo terminal (this session): PRE-ENTRY CHECKS OK; risk approved quantity 0.25, notional 6381.29 EUR, leverage 12.76x (ceiling 30), binding constraint risk_budget (15.00); bracket built; the adapter's real order_check returned 'Done'; order_send not called; book flat and RECONCILED(VENUE_SNAPSHOT) afterwards. RESTART PROOF mechanics on the real terminal (flat book): NOT_RECONCILED before any comparison -> mass status -> broker snapshot -> RECONCILED(VENUE_SNAPSHOT). The restart proof after a real lifecycle must be re-run once the user has executed `--live`.
- Fake-broker tests (test_c7_demo_slice.py) run the identical code end to end: min-lot bracket with attached stop, controlled reduce-only close, Nautilus PnL == broker PnL, fresh VENUE_SNAPSHOT reconciliation, restart proof, every STOP condition, risk veto, adapter second gate (invalid stop denied before the broker), dry-run, and the expected-vs-actual report.
- Observation for later research (not acted upon): min lot risks ~10 EUR (2% of the 500 EUR demo balance) with a 40-point stop; the effective index margin is 1:20 (319 EUR per min lot), i.e. ~64% of the demo balance is margin for one min-lot position.

### C6/C6.5/C7 Codex review (one read-only pass) and fixes
FIXED: (Critical, defense-in-depth) account identity/demo status was only checked at start: the session now pins the expected login and, with `require_demo_account`, every exposure-changing `order_send` / SL-TP change / protective cancel re-reads `account_info` and refuses on a changed login or non-demo `trade_mode` (reconciliation invalidated; the C7 runner enables it). (High) a re-delivered ClientOrderId can never reach the broker twice (`DUPLICATE_CLIENT_ORDER_ID_ALREADY_RECORDED`). (High) protective cancel: a raised/None/ambiguous `order_send` is an UNKNOWN outcome -> reconciliation dropped, then the position's SL is verified at the broker (removed => CANCELED, present => cancel_rejected); a definite refusal keeps authority. (High) bracket protection levels are compared on every reconciliation: Nautilus' authoritative SL (and TP) vs the broker's => `PROTECTION_LEVEL_MISMATCH` (blocking). (Medium) `run_slice(account_leverage=...)`: the C7 runner passes the broker-reported account leverage as the cap (a 10x cap correctly vetoes the min lot on 500 EUR); the ceiling is never assumed when a real value is known.
ACCEPTED / NOT CHANGED (reasoned): (High) 'late lane completion overwrites IN_DOUBT with ACCEPTED': the late completion reports what the broker actually answered (DONE); authority stays dropped (session degraded, reconciliation invalidated) and the remaining steps fail closed until an attach-only reconnect + explicit reconcile books the deals (proven by the 0.6 s order_send test). (High) restart proof with an OPEN position: the plain ExecutionEngine used by the runner does not apply mass-status reports; the adapter correctly reports MISMATCH rather than trusting persisted state, and Nautilus' LiveExecutionEngine reconciliation is proven in tests. The required C7 restart proof runs after a completed (flat) lifecycle. Deferred: wiring `LiveExecutionEngine.reconcile_execution_mass_status` into the runner for restart-with-open-position.
Codex reported nothing for threading/LoopBridge, per-symbol serialization, request shapes, fill dedupe, duplicate authoritative state (unclear whether clean or not reached). No second confirmation pass run; fixes pinned by test_c7_review_fixes.py. A dry run and restart-proof were re-run on the real demo terminal after the fixes: both OK (order_send never called).

## C7 FINAL RECORD (user-executed real ActivTrades DEMO slice, 2026-09-29)
GER40/Ger40, 0.25 lot, effective leverage 12.7584x (policy max 30x). Entry expected ask 25504.07, actual 25504.82 (slippage 0.75 pt; decision spread 2.04, end spread 1.79). Expected stop 25462.03 == broker stop 25462.03. Expected margin 318.81 EUR == broker peak margin 318.81 EUR. Latency entry 93.1 ms / exit 48.6 ms. PnL Nautilus -0.99 EUR vs broker -1.00 EUR. Final broker: positions 0, working orders 0; final Nautilus: open positions 0, open orders 0; VENUE_SNAPSHOT -> RECONCILED. Restart proof: NOT_RECONCILED -> fresh venue snapshot -> RECONCILED.
RESTART `mass_status.positions = 1` INVESTIGATED (Codex read-only, model: Codex): NOT a broker-position defect. The value is `len(mass.position_reports)` (scripts/c7_demo_vertical_slice.py:100-106); the adapter emits one explicit FLAT (qty 0) position report per registered instrument so Nautilus can close a stale local position (src/nautilus_mt5/execution_client.py ~1738-1749). Broker truth in demo_slice_report.json is flat. Reporting label to be renamed (position_reports vs open positions) - cosmetic, deferred.

## AR1 Alpha Research Phase 1 - GER40 (2026-09-29..30) - RESEARCH ONLY
BASE 1648b48. Commits: AR1A f47c710 harness+pre-registered config, AR1B 3997d49 downloader, AR1C/D/E e3291d8 strategies+lookahead tests, AR1F-1 d1113d8 review fixes+runner, AR1F-2 a74e351, AR1F-3 6deaf60 outputs. Models: lead Sonnet (direct, sequential harness build); Codex read-only review (1 pass, no Critical); Codex write fixes in isolated worktree (H1-H3,M4,M5,L8); Codex read-only C7 analysis. Gemini/Opus not used.
DATA: 19 validated months 2025-02..2026-08, 96,219 M5 BID bars + broker spread (train 50,705 / validation 25,132 / OOS 20,382 bars). Months 2024-01..2025-01 rejected by validation (no usable history). Admitted with reviewed warnings: SPREAD_ANOMALY (kept, real costs) and 4 documented gaps (Easter x2, Christmas, one 45-min hole outside the trading window). Session mapping verified (activity peaks 09:00 and 15:30 Berlin in winter and summer). Splits (chronological, no shuffle): TRAIN 2025-02..2025-11, VALIDATION 2025-12..2026-04, OOS 2026-05..2026-08. Development ran on a frame physically truncated before OOS.
DESIGN: fill at NEXT bar open on executable side, stop-first inside a bar, gap stops at the open, forced flat 21:30 Berlin, fixed-R sizing (50 EUR risk, 0.25 lot step, 10x research cap), costs base spread + 0.5pt slippage/market fill, 4 stress scenarios, commission 0 (observed), swap n/a (no overnight).
RUNS: 36 signal configs x 4 exits = 144 dev variants (164 simulations incl. 20 final cost-stress runs). OOS gate: one frozen set; two logged resets (framework fixes; lint-only re-run), never strategy changes.
RESULTS (see research/reports/ar1_phase1/report.md): Pre-registered canonical MOMENTUM, BREAKOUT(ORB), PULLBACK all REJECTED. Only grid-selected BREAKOUT ORB (30-min OR, 0.25 ATR buffer, OR-mid stop, fixed 2.5R) is RESEARCH_CANDIDATE, WEAKLY: OOS 87 trades, expectancy +0.018 R, day-clustered 95% CI [-0.28, +0.34], PF 1.01 - statistically indistinguishable from zero, best-of-40 selection, ~1 trade/day. Momentum: strong train, negative validation (overfit pattern). Pullback: negative validation/OOS. Only 2 of 48 variants with >=2 trades/day are positive in both train and validation: the 3-8 trades/day cadence is not supported by current evidence.
CODEX FINDINGS FIXED: H1 OOS gate now binds full experiment fingerprint (candidates+config+dataset hashes+source hashes) with logged reset; H2 entry-bar gap fills at the open; H3 adverse entry gaps kept as ENTRY_GAP_STOP trades (no survivorship skip); M4 MFE stop-first; M5 holding time; M6 OOS PF check + selection note; L8 day-clustered CI. Results unchanged in verdict after the fixes.
TESTS: alpha 52; full suite 1693 passed 1 skipped; ruff clean; compileall OK; 0 real MT5 calls in tests.
DEFERRED: Monte Carlo sequence stress, walk-forward (needs longer history), Partial TP/exit research, dynamic sizing, NASDAQ100 (symbol not yet observed), M1 intrabar order resolution, Gemini adversarial pass (not needed: Codex found no Critical), rename of C7 `positions` report label.

## AR2A — Alpha Research Phase 2 start (2026-09-29)

- Branch: `sprint1/integration`; starting HEAD: `fe1bbc2da09dcaa6cb187c5f821183fa7d5435b3`; working tree clean.
- AR1 state pushed to `origin/sprint1/integration` as a fast-forward (`068c3c8..fe1bbc2`, 65 commits); no remote divergence (fetch showed 0 behind), nothing overwritten.
- Orchestration: Claude/Sonnet = orchestrator/integrator; implementation delegated to Codex, cheap recon to scout/Haiku.

## AR2 progress + RESEARCH ENGINE REBUILD decision (2026-09-29)
COMMITS (all local, branch sprint1/integration, NOT pushed since AR1 push fe1bbc2): AR2A 0d7a76c MTF layer, AR2B c142cd9 H1 regime + M15 context, AR2C 11cd58f SignalCandidate/bridge/conflicts, AR2F 2b6079e group C, AR2D 5699117+08bc7dc group A, AR2E f747560 group B, 3fde3e2 perf checkpoint + runner + 12k golden reference, 8e19dcc/a2ab003 research dep group (numba 0.67.0, TA-Lib 0.8.1, default group).
FINDING: the AR2 mass-screen loop (per strategy x per bar Python MtfState) is architecturally too slow: 22 variants x ~70k bars took 40+ min silent runs (killed/unfinished); optimized 12k-bar run 91 s -> 28.4 s. Runner (research/runners/ar2_compare.py) has never completed on real data. DECISION: stop patching it; build Feature Store (persistent, cache-keyed) + numba fast screen + fast simulator; Nautilus only for survivors.
EQUIVALENCE: Opus perf changes (label cache, MtfView.at, positional label lookup, opening_drive) PROVEN identical: all 22 variants / 1311 candidates == pre-optimization reference on the 12k Train->Validation slice (research/reference/ar2_ref12k/golden.pkl). Retained.
DEPENDENCY SPIKE: numba 0.67.0 + TA-Lib 0.8.1 install and run under numpy 2.5.3/pandas 3.0.6 (Windows py3.12, no compiler); numba 1000 loops over 70k bars = 0.067 s. DECISION: NumPy/Numba direct (no VectorBT: stateful strategies, extra deps, duplicated fill semantics); TA-Lib for features only; Optuna 5.0.0 / DEAP 1.4.4 resolve but are deferred. Caveat: numba DLL fails in very long Windows paths (scratchpad) - keep test paths short.
DELEGATION: Codex A1 Feature Store, Codex A2 fast simulator (parallel, separate worktrees ar2-worktrees/fstore, fsim). Claude only orchestrates/verifies. Gemini red-team + Codex review planned after the kernels exist.

### Rebuild step A done (b92d952, 59b5ee8): FeatureStore + numba fast simulator + registry
FeatureStore: 84 causal arrays on the 75,837-bar dev frame; Codex-reported cold build 26.6 s (incl. compressed cache write), cache load 0.22 s, 8.4 MB (Claude-measured on the 12k slice: 2.7 s build). Fast simulator EXACT vs AR1 `evaluate_candidates` on all 22 golden variants x all cost scenarios (entry/exit idx, exit reason, R/PnL 1e-9). Bugs found and fixed by Claude after Codex hit its usage limit (reset 21:49): (1) decision-bar mapping was one bar late (signal_ts is the bar CLOSE), (2) slope edge case on series shorter than the lag. LESSON: worktrees lacked data/ar1_ger40, the golden test SKIPPED and hid bug (1); data now copied into kernel worktrees and skips must be reported.
GEMINI red-team (design text only, via agy in snapshot worktree, 27 s): keep optimization/ML out until the 22-variant baseline exists (adopted; matches hard stop); cascade timestamping for H1/M15 (pinned by store tests); count every evaluated trial (deflated-Sharpe style) and cap search budgets, consider embargo between Train and Validation (adopted as TODO for the search phase). REJECTED/CORRECTED: claim that stop-first inflates win rates - it is the pessimistic direction; optimistic bias comes from next-bar-open fills without spread widening, covered by cost-stress scenarios.

## AR2 research-engine rebuild - RESULT (2026-09-30), final code d7c616d
Verdicts: engine rebuild COMPLETE (fast core); 22-variant Train+Validation COMPLETE (68 s cold); candidate equivalence PASS (12k + full 75,837-bar frame, 22 variants / 8650 candidates bit-exact); performance target PASS for 22 x 70k (1000-variant needs a light screening mode); PROVEN EDGE FOUND: NO.
Commits: 3fde3e2 perf+runner+reference, 8e19dcc/a2ab003 research deps, b92d952 FeatureStore+fast sim, 59b5ee8 registry+helper, 8be0632/dad0947/e535fa5 kernels A/B/C, a63e803 fast runner, 84bb402 review fixes + full-frame gate, d7c616d alpha boundary (StrategySpec/AlphaProvider).
Codex review (1 pass): 2 High (cache fingerprint missing sizing/rules; OOS accepted stale frozen provenance), 3 Medium (mean-reversion bucket reconstruction; unmasked restricted stress; unflagged regime mining), 1 Low (library versions in cache key) - all fixed. Claude fixed after Codex usage limit: decision-bar mapping off by one bar (hidden by a SKIPPED test in a data-less worktree), short-series slope edge case.
Findings: no variant passes the freeze rule whole; 5 REGIME_RESTRICTED survivors are validation-mined best-of-many (hypotheses, not evidence). Context question: of 199 qualifying slices only 16% positive in Train AND Validation (chance ~25%); mean slice expectancy -0.098R Train, +0.000R Validation. SESSION_TWAP_REFERENCE is untestable as defined (all candidates outside the entry window). Busiest variant 1.77 trades/day; 3-8/day is not supported. Trial accounting recorded in dev_run_record.json.
Full pytest 1848 passed, 1 skipped; ruff clean. OOS never touched (no oos_access_log exists).
DEFERRED: Optuna, DEAP, Qlib/RD-Agent spike, RL, NASDAQ100, exit research, light screening mode for 1000+ variants, Nautilus validation of survivors, walk-forward, Train/Validation embargo, new SESSION_TWAP version, push of the AR2 commits.

## AD1 — Automated Alpha Discovery V1 start (2026-09-29)

- Branch: `sprint1/integration`; starting HEAD: `8e9f97b87aecb0518153458099c22f83e1e15f86`; working tree clean at start.
- AR2 state (22 commits, `fe1bbc2..8e9f97b`) pushed to `origin/sprint1/integration` as a pure fast-forward (0 behind / 22 ahead verified after fetch); nothing overwritten, no local commits lost.
- Phase plan: AD1A embargo + light screen, AD1B 100/1000 benchmark, AD1C Optuna, AD1D DEAP/grammar, AD1E discovery run, AD1F Nautilus survivors, AD1G review fixes + report. OOS stays sealed. Claude = orchestrator/integrator; implementation via Codex, recon via scout/Haiku.

## AD1 — Automated Alpha Discovery V1: progress record (2026-09-30)
COMMITS (pushed through 8df8d87 on origin/sprint1/integration): AD1A embargo(5d)+light screen; AD1B benchmark (1000 real-kernel variants ~4 s, gate <=5 min PASS); AD1C 29 causal price-action features (truncation-invariance tested) + shared evaluator/Train-only fitness/Optuna; AD1D grammar (catalog/genome/compile/canonical hash/trial ledger) + DEAP + campaign runner; AD1E stages C-E, selection stats, overlap clusters, verdict rule, survivors report; fixes (unique-spec accounting, cumulative trials, brk floors, degeneracy audit, sample-size guards, entry-timing robustness, configurable Train minimum); null calibration.
DELEGATION: Claude=orchestrator; implementation by Sonnet builders (Codex weekly quota exhausted mid-phase); Haiku scouts for recon; Gemini (agy, snapshot worktree, tool-free, `pro`) adversarial review; Opus auditor for the final read-only correctness audit.
CAMPAIGNS (Train+Validation only; OOS never read by AD1 code): main1 9,925 unique specs (looser gates: 28 Stage-E survivors, 21 clusters, best pooled t 3.48 < null bound 4.29 -> INCONCLUSIVE; under current gates only 2 finalists, Val t 1.18/1.08); main2 9,946 unique (1 finalist: TREND_PULLBACK, Train E +0.18R n=113, Val E +0.20R n=52 t=1.13, pooled t 1.84 vs null bound 4.45; degrades gracefully under 1-3 bar entry delay); main3_hf (>=250 Train trades, ~1.2+/day) 9,927 unique: 0 Stage-C survivors -> NO. Cumulative 30,310 trials / 29,798 unique specs.
NULL CALIBRATION v1 (block-shuffled returns, 3 seeds, drift-preserving): Stage-E finalists on structure-free data in 2 of 3 seeds (10, 5; Val t up to 1.87, pooled t up to 2.76): real main2 lies inside the null range. Pipeline gates alone are NOT a discovery criterion; only the selection null bound rejects them.
OPUS AUDIT: no lookahead / Train-Validation leak found (115 arrays bit-identical on prefixes). HIGH evidence caveats: (1) OOS 2026-05-01..08-31 was already evaluated in AR1 (oos_access_log: 3 evaluations) -> NOT a clean holdout, a new forward period is required for a final test; (2) Validation reused across AR1/AR2/AD1 campaigns, N undercounted; (3) no intraday-drift baseline while nearly all finalists are LONG; (4) null v1 biased+underpowered. Follow-up batch dispatched: provenance checks, cache-fingerprint gaps, canonicalisation fixed point, partition-wise Stage D, Validation-only verdict statistic, drift baseline, zero-drift 12-seed null.
GEMINI REVIEW (judged by Claude): accepted: Validation is a survival filter (partly in-sample); entry-delay perturbation; frequency experiment. Rejected: claim that subtracting SE favours few trades (backwards); locking a candidate + running OOS (violates the hard stop); missing volume/intermarket data (unavailable) and time-of-day features (already a gene).
DEFERRED: AD1F Nautilus survivor validation (no finalist warrants it; needs SignalCandidateStrategy adapter + BacktestResult->TradeArrays exporter, cost-model parity work); Qlib/RD-Agent spike; walk-forward; exit research; dynamic sizing; RL.
OOS TOUCHED BY AD1: NO (previously viewed at project level by AR1 - see above).

## AD1 — closing addendum (2026-09-30)
Pushed through 75a1c78 (audit fixes + REPORT.md). Then: drift/random-entry baseline and POWERED null calibration completed (12 zero-drift block-shuffle seeds, 4 sign-flip-day seeds, same campaign settings as main2, cumulative N 20,199). Result: real campaigns are at or BELOW the null (best Val t 1.13-1.18 vs null medians 1.5-1.7; best pooled t 1.84-2.00 vs null min 2.13). main2 finalist beats random entries (+0.34R Train p 0.004, +0.38R Validation p 0.022) so it is not pure drift, but that is not evidence of an edge at 800 candidates / ~30k trials, and Validation t 1.13 < 2.5. Verdict: NO ROBUST EDGE FOUND; rule verdict INCONCLUSIVE. OOS untouched by AD1 (previously viewed in AR1).
STATE: V1 COMPLETE except deferred Nautilus stage F. Evaluator speed optimisation (lazy BASE cost etc.) runs in a separate worktree, merge only if bit-identical. V2 is planned (directional context flags, more markets/frequency, unseen forward period).

## AD2 — Alpha Discovery V2 start (2026-09-30)
- Branch sprint1/integration; starting HEAD ce89177 (= origin, tree clean). V1: complete, no robust edge, 29,798 unique specs / 30,310 trials cumulative (carry these counts forward; never reset trial accounting).
- OOS 2026-05..08 is NOT a pristine holdout (viewed in AR1); a new unseen forward period is required after strategy freeze. Never call the old window untouched OOS.
- Workstation Dispatcher is OUT of the plan (pydantic/env failure); direct worker delegation only: Claude/Sonnet orchestrator, Codex (engineering; quota returned 02:51), Opus (temporal-engine design, final audits), Gemini (adversarial research review), Haiku scouts (recon), isolated worktrees under ad-worktrees/.
- Disk: C: only ~6.8 GB free at start (V1 filled it via parallel nulls with per-seed caches + one-file-per-trial results). Disk safety (bounded caches, run-scoped scratch, free-space guard) is part of Phase 0.
- Phase 0: speed worktree (WIP e8dcdb1 on top of ce89177, unverified) handed to Codex to finish + prove bit-identical + add disk safety.
- Lanes started: A speed/disk (Codex), B chart intelligence (Phase 1 fixes: cash-session levels, directional context, session-conditioned thresholds; Sonnet builder; Opus design of temporal event engine/state machines), C multi-market data recon (Haiku scout), external-repo classification (Haiku scout).

## AD2 progress checkpoint (2026-09-30, HEAD 8912814 locally; origin at c0814d6)
- Phase 0 DONE (Codex): evaluator 4.57x cold / 3.62x warm on 800 genomes, bit-identical candidates/hashes/repr(train_fitness); smoke campaign 594 candidates identical; survivors C15/D15/E1 INCONCLUSIVE; disk safety (500 MB bounded cache, 2 GB free-space guard, run-scoped cleanup).
- Phase 1 DONE: cash-session levels (no overnight), directional context flags, session-quantile rank features (opt-in, Train-only tables); V1 arrays bit-identical; FEATURE_SET_VERSION 3.
- Temporal engine: registry + StateMachineStrategySpec, streaming oracle, numba all-instances NFA kernel (parity exact vs oracle; 206 specs/s @4 workers synthetic, 227 real), real EventSet (now 439 arrays, events-v2.2), real-data truncation/perturbation/DST tests clean. Opus lookahead audit: NO leak; fixed: stale evaluator cache (fail-closed key now), ZONE_ENTER wrong-zone capture (0 violations), SHORT feature quantile-mirror -> exact price mirror, min_space_r at the fill, workers>1 lazy frames.
- Simulator: finite targets crossed at fill skipped (target_crossed_at_fill) and space_below_min_at_fill; live executor must implement the same pre-fill checks (parity requirement documented in sim.py).
- Multi-market: MarketSpec + 5 markets (Ger40, UsaTec, Usa500, GOLD, EURUSD resolved on demo), data capped by terminal at 100k bars/timeframe (M5 ~16 months, M1 ~2.5 months); forward holdout guard: nothing after 2026-08-31 enters dev frames; per-market SimWindow/cost/sizing (GER40 bit-identical). Calendars of 4 markets PROVISIONAL.
- FINDING: at 500 EUR with 10x research cap a min lot needs 12.7x (GER40) / 10.7x (NAS100): all index trades size_below_min; discovery therefore runs on a normalised 10,000 EUR research account, 500 EUR feasibility is a separate annotation.
- FormulaAlpha (own implementation; AlphaGen has no licence; RD-Agent classified Linux/Docker-blocked by documentation only, NOT install-tested; Qlib heavy): GP vs random on GER40 Train: best fitness 0.067 vs 0.073 (null <= 0.058), expectancy ~0 at BASE and -0.11 R at adverse cost: no edge.
- Open: real multi-market probe (1-2k candidates/market), confluence measurement, ML meta-label, Gemini adversarial review, exit research, growth simulator, Nautilus Stage F (deferred: no finalist), clean forward holdout (from 2026-09-01) untouched.

## AD2 FINAL (2026-09-30): see docs/V2_FINAL_REPORT.md
- Engineering complete and pushed (5e45e5b, 3,087 tests green); survival stage executed ONCE on frozen finalist hash 34aba8bf...; results in research/reports/v2_survival/.
- Discovery verdict: NO robust edge; all 5 markets' best-of-1500 indistinguishable from 63 structure-free null runs; survival: 8/150 positive (chance ~10), 0 with t>2, mean -0.315 R.
- Not run / open: RD-Agent+Qlib (docs-only blocker classification), LLM hypothesis loop, dynamic sizing, exit research, Nautilus Stage F, provisional calendars of 4 markets, ML libs not installed (owner approval).
- Clean forward holdout (>= 2026-09-01) UNTOUCHED. Cumulative trials in the survival ledger: 40,443.

## DEMO TRADER phase start (2026-09-30) — results first
- Branch sprint1/integration, start HEAD e5f1ee3 (clean). Scope: ActivTrades DEMO only; 30x hard broker-leverage cap; live.py inert. Workstation Dispatcher NOT used; direct delegation (Sonnet lead, Codex engineering, Opus read-only audit, Gemini adversarial review, Haiku scouts).
- Workstation at start: C: 15.0 GB free, RAM 8 GB total / ~0.25 GB free (many stray python procs from earlier sessions) -> no parallel heavy jobs.
- Pipeline: MARKET -> OpportunitySnapshot -> Risk -> DEMO order (broker stop) -> outcome -> dataset -> learning (shadow champion/challenger).
- Recon (Haiku scouts, read-only): exec adapter API, live causal signal reuse, risk/persistence/monitoring. Lead handled: repo/disk/RAM check, ledger (directly, trivial).
- ADDENDUM (binding, precedence over master prompt): (1) Discovery-Demo data is NOT a clean holdout; separate FROZEN-DEMO phase later (frozen champion/policy, no retraining); every record carries `phase` = DISCOVERY|FROZEN so both are separately identifiable. Historical holdout >= 2026-09-01 stays untouched. (2) No Optuna/DEAP/FormulaAlpha/LLM/research runner in the hot path; demo trader deterministic, runs without any AI agent. (3) Per market before DEMO AUTO: verify UTC -> market tz -> DST -> local trading minute -> broker session/calendar -> SimWindow; never Berlin minutes on NY markets; calendars stay PROVISIONAL until broker-confirmed. (4) Demo execution mirrors validated simulator: target_crossed_at_fill, min_space_r on ACTUAL fill, stale/already-crossed entry, structural invalidation. (5) Always record spread+slippage; verify broker commission; record swap for overnight; unconfirmed cost assumptions flagged provisional. (6) MLflow local-only (sqlite/file store, localhost UI, no secrets logged). (7) DoD adds: DISCOVERY DEMO DATA AND FROZEN FORWARD DATA SEPARATELY IDENTIFIABLE: YES/NO.
- Contracts committed 4c3f2b4 (src/demo/contracts.py, docs/DEMO_TRADER.md). Lanes dispatched (worktrees demo-worktrees/lane{b,c,d}, branches demo/lane{b,c,d}, base 4c3f2b4): Lane B opportunity engine = Sonnet builder; Lane C multi-symbol demo execution + scripts/demo_trader.py = Codex (--write, isolated worktree); Lane D store/labeling/report = Sonnet builder. Learning stack (Logistic/LightGBM/River/MLflow) after demo runs. Opus read-only audit after integration; Gemini adversarial review planned.
- Stage-1 preflight (existing scripts/c7_preflight.py, read-only, attach-only) 2026-09-30: exit 0, DEMO account, equity 499 EUR, flat book, Ger40 quote fresh (spread 1.96), terminal connected. Sizing note: at 499 EUR, 1% risk = ~5 EUR; GER40 min lot 0.25 => stops > ~20 pts exceed 1% (expect many `size_below_min` rejections; recorded, not hidden).
- MERGED to sprint1/integration: Lane D (store/labeling/report/export, 67463ad), Lane L (shadow learning: LogReg/LightGBM/River/local MLflow, deps group `learning` now in default-groups, bc1ba6b), Lane B (causal OpportunityEngine + static-demo-policy-v1 + frozen production spec v1 hash c3eae99e782888ac, e88a3df). tests/unit/demo: 245 passed. Lane B flow estimate on dev bars: ~60 valid opportunities/day over 5 markets (ROUND ~50-60% of flow), ~28/day after one-position gating; live quote will lower it. Lane C (Codex, execution + scripts/demo_trader.py) in progress. Models: B,D,L = Sonnet builder; C = Codex.
- Codex weekly quota EXHAUSTED mid-Lane C (resets Oct 4 01:49); its WIP committed on demo/lanec (1d4a946: executor validation, market_config, bar source, ports, CLI skeleton, 20 tests). Finding by Codex: NautilusRiskBridge has hardcoded GER40 account state -> cross-owner generalisation authorised by Lead (bridge only; src/risk minimal + failing test first).
- StackPort contract (src/demo/execution/stack_port.py) decouples runner from live stack. Dispatched: Lane C2 (Sonnet builder, worktree lanec): Mt5DemoStack, DemoTraderStrategy, demo-discovery-policy-v1 (1% risk, min-lot allowed up to 2%, 30x hard cap, daily loss 6%, DD 25%, cluster INDEX/METAL/FX total open risk <=4% cluster <=3%), LiveBarSource. Lane C3 (Sonnet builder, worktree laner): DemoRunner loop, monitor/heartbeat, milestone reports, CLI, FakeStack. Both Sonnet (Codex unavailable). Next: merge, integrate, Opus audit, shadow, canary.
- USER RISK CORRECTION (binding): fixed 1%/2% sizing rule REJECTED. New: structural stop (never moved) -> min lot/step -> actual EUR/equity risk -> leverage (30x hard) -> portfolio/cluster -> configurable hard safety caps -> trade/skip. size_below_min = min lot violates a safety cap. Log full risk detail on every accept/reject. Expectancy over series, not winrate; no probability filters; model probabilities shadow only. Lane C2 (agent a55e204ca3ad1aa62, active, commits 20582db 22500cb) was sent the new spec; NOT merged; C3 (368997e on demo/laner) to be adapted to C2's new risk-detail fields afterwards.
- PROFESSIONAL-PRACTICE ALIGNMENT (user, binding): risk != alpha layers; no universal fixed risk %; portfolio instead of GLOBAL one-position (per-symbol netting stays as STRUCTURAL); signal count != risk allocation (track family/market/cluster concentration); NO trade quota (10+/day = throughput goal, never an admission gate); expectancy > winrate; hard controls = demo-only/account/recon/stale feed/broker protection/leverage+margin/catastrophic limits/duplicate safety/kill switch; quality inputs (confidence/confluence/family score) log/rank/soft-filter; vol/forecast-scalable sizing hook, no aggressive scaling; broker stop = safety, strategy exit = alpha (TP not mandatory); TCA per trade (spread, slippage, fee, fill quality, latency, movement-to-cost). Required BEFORE final merge: TRADE-SUPPRESSION / PROFESSIONAL-PRACTICE AUDIT classifying every gate SAFETY|STRUCTURAL|QUALITY|LEGACY/ARBITRARY (remove arbitrary, quality only hard if justified) + REJECTION FUNNEL. C2 sent items (GATE_CATALOG etc.); Lane B policy gates (min_space_r 0.25 / entry_tolerance_atr 0.5 defaults, ROUND dominance) to be audited after C2.
- NETTING PRECISION (user, binding): broker = one net position per symbol; INTERNAL = multiple signal/tranche records may contribute to one net position within portfolio/cluster/leverage/margin/safety limits. A same-symbol open position must not reject a valid setup as a "risk rule". If add-on is unsupported in v1 (MT5 net position has ONE SL/TP => tranches with different stops can't be independently broker-protected) it is an explicit TEMPORARY STRUCTURAL LIMITATION (`ADDON_EXPOSURE_NOT_SUPPORTED_V1`), counted separately in the rejection funnel, not presented as a professional risk rule. Lane B code ONE_POSITION_PER_INSTRUMENT to be renamed/reclassified accordingly in the gate audit. Sent to C2.
- MERGED to sprint1/integration: Lane C2 (Mt5DemoStack, DemoTraderStrategy, DemoPositionSizer + RiskCaps, GATE_CATALOG, tranche ledger, LiveBarSource; 7b064a2), lint allowlist commit (demo/opportunity may import frozen alpha kernels only; architecture decision in ARCHITECTURE.md), Lane C3 runner (368997e) -> HEAD 92a28ab. C2 full suite: 3362 passed, 34 failed + 35 errors (env: gitignored data/ar1_ger40 absent in worktree; 2 arch-lint tests fixed by allowlist). 
- Dispatched: Lane I (Sonnet builder, worktree lanei): wire runner to real Mt5DemoStack, persist risk_detail/TCA, rejection funnel, opportunity-policy trade-suppression fixes (remove ONE_POSITION gate from policy, min_space_r default 0). Opus read-only execution safety audit (agent ad9905be...) running on 92a28ab in parallel.
- 2026-09-30 evening: Opus execution audit (verdict NO-GO until fixed) -> Lane F merged (H1 entry tolerance, H2 emergency reduce-only in any recon state [parity scenario now EXPECTED_DIFFERENCE, contract amended], H3 deterministic in-doubt, H4 no-trace resolution after restart, M3 own-trade daily consecutive losses, M4 skew handling, M6 read retry, M5 server-tz self-check, L1, L4); Lane I merged (runner on real Mt5DemoStack, one BarSource interface, risk_detail+TCA tables, funnel, IN_DOUBT state, transient-condition handling, learning off in demo-auto, opportunity-policy audit: ONE_POSITION gate removed, min_space_r default 0; OOS engine flow ~85 accepted/day over 5 markets before stack gates, ROUND 52% of flow). Full suite at merge: 3680 passed, 1 skipped, 2 parity failures (fixed -> 23/23 parity green), ruff+compileall clean.
- First real-terminal SHADOW attempt: fail-closed `unexpected_server` (correct behaviour): terminal reports account.server 'ActivTradesEU-Server' (trade_mode 0 DEMO) vs .env display value; pinned observed server as identity tripwire (DEMO-ness proven by trade_mode==0 + expected login). Shadow run 2 (14:42 UTC): attached, RECONCILED, equity 499, feeds fresh, clock chain per market ok (4 markets calendar PROVISIONAL).
- Lane S (Sonnet builder): shadow mode exercises the stack's risk/sizing gates via dry-run submit (no order_send) so shadow funnel matches demo-auto.
- 2026-09-30 15:35-15:46 UTC DEMO AUTO CANARY (artifacts/demo_auto, phase DISCOVERY, ActivTrades DEMO acct equity ~499 EUR): first real DEMO trade GER40 BUY 0.25 lot fill 25177.6 (int-053ea42734f29c80, broker ticket 5038887553): PLANNED->RISK_APPROVED->SENT->FILLED->PROTECTED, broker stop confirmed, ALL_PROTECTED. Structural-stop sizing at min lot: stop 38.4-40.5 pts, ~9.6 EUR risk = ~1.9% equity, leverage ~12.6x (accepted per user risk semantics). Earlier NAS100 intent RISK_REJECTED spread_cap (spread 2.1 > p99 bar cap 1.88; live bar-close spreads ~2x bar median -> cap suppresses NAS100; relative-cost rule proposed, edit denied by permission classifier, awaiting user decision). Third intent (GER40 VOLREV) RISK_REJECTED size_below_min: liquidation_safe_leverage 24.99 > 15.09 with existing GER40 exposure.
- Incident: heartbeat os.replace failed (WinError 5) because my monitor read heartbeat.json concurrently -> runner fail-closed halted NEW exposure (position stayed protected/managed). Fix: write_heartbeat retries 10x with backoff, runner fail-closed only after 120 s persistent failure (tests). Monitors now read a copy.
- RESTART PROOF (real terminal, open protected position): runner stopped via STOP file, restarted on same artifacts dir: new pid, RECONCILED from venue snapshot, position re-adopted with same intent_id PROTECTED, no duplicate order, no exposure change. PASS.
- 2026-09-30 16:46 UTC first trade closed (GER40 short, 66 min, MANUAL close at user request): net -0.19R, -1.94 EUR, MFE +0.67R, MAE -0.78R; outcome recorded end to end (exit reason MANUAL/EXTERNAL path verified on the real terminal). Spread gate changed per user: relative cost (<=20% of 1R) is primary, absolute p99 bound only 4x safety cap.
- 499 EUR account finding: margin per min lot GER40 ~315 EUR, NAS100 ~270, Gold ~367, EURUSD ~33 => only one index/gold position fits; user switched to a new DEMO account (balance 100,000 EUR, leverage 1:30, same server name, login matches .env; the assistant did not enter credentials). New data store: artifacts/demo_100k (separate from artifacts/demo_auto = 499 EUR account; do NOT merge the two stores in analyses without an account tag).
- 2026-09-30 ~17:00 UTC user-requested MIN-LOT TEST TRADES on the new 100k DEMO account (runner paused while flat; direct MT5 scripts, login/DEMO verified, broker SL+TP set in the same request, closed after ~15 s, book verified flat): GOLD 0.01 lot BUY fill=ask (slippage 0), latency 33 ms, margin 366.93 EUR, spread 0.51 USD, result -1.45 EUR (USD->EUR converted), commission 0.0, swap 0.0 (intraday), swap_long -73.6 / swap_short +28.16 (broker units); UsaTec 0.2 lot BUY fill=ask, latency 24 ms, margin 269.69 EUR, stops_level 75 pts, spread 2.11, result +0.04 EUR, commission 0.0, swap 0.0. => ActivTrades DEMO charges NO commission on these CFDs (spread-only); swap only when held overnight (cost_status can be 'verified: commission=0'). These two test trades are NOT in the DemoStore (manual scripts).
- 2026-09-30 17:20 UTC CONSOLIDATED MASTER PROMPT received (supersedes earlier addenda: risk/netting/suppression/sessions/closed bars/decay/fast/TCA/DOM/data quality/learning/delegation/ops). State: 100k DEMO runner continuous (pid 30188), RECONCILED, NAS100 short 28.2 lots open (risk 995 EUR = 0.995% equity, lev 7.6x, protected). Finding: SPX500 short rejected margin_stop_too_close_to_liquidation after the sizer's liquidation fit (duplicate/contradicting gate). Broker check: all 5 symbols trade_mode FULL with fresh quotes at 17:20 UTC incl. GOLD/EURUSD outside their London 08-15 strategy window => strategy window != broker session (MT5 python exposes no session schedule).
- Dispatched: Opus read-only gate/duplicate-gate/session/closed-bar/accounting audit (agent a42c85b1...); Lane R2 (Sonnet builder, worktree laner2, base 444b69d): closed-bar catch-up (never late-chase), missed/expired logging + counterfactuals for all non-traded, funnel per gate, MANUAL censored / CANARY excluded / account-phase + account hash guard, overnight/weekend idle, TCA fields. Merged lane worktrees removed to save disk.
- 2026-09-30 evening MERGED: F2 (sizer uses evaluator's reference price -> fitted size always passes margin check [was 3/3 live rejects], canary magic 740099 isolated, SAFETY_FLATTEN exit hint, gate annotations, configurable stop-out fraction default unchanged) d0feef1; R2 (persisted closed-bar cursor + chronological catch-up never traded late, scan not gated on can_trade, seen id committed with snapshot, counterfactuals for all non-traded, per-gate funnel, censored/canary/account-hash+phase, closed-market idle vs fault, TCA chain) ad2f0c1. Full suite at ad2f0c1: 4609 passed, 1 skipped, 1 failed + 4 errors: the 4 rawscan errors are MemoryError on the 8 GB box (pass in isolation), the 1 failure (test_bounded shutdown) was a test-isolation flaw (global MT5 lock held by the live runner) -> fixed (own lock file). Runner restarted on merged code (pid 22668): account_phase ALPHA_EXECUTION_DISCOVERY, account_id_hash bound, per-market states FRESH/OPEN_OUT_OF_SESSION.
- 100k-phase strategy trades so far (n=3, all STOP): NAS100 short VOLREV -1.001R (-1037 EUR, MFE +0.61R); SPX500 long ROUND -1.001R (-996 EUR, MFE +0.18R); SPX500 short ROUND -1.034R (-1017 EUR, MFE 0.0, 8 min). Cum -3.04R, equity 96,949. No statistical meaning at n=3. Open items: out-of-window counterfactual generation (needs alpha/families lane), Gemini adversarial review, OUTSIDE_ENTRY_WINDOW currently unobservable, liquidation model decision (mm = full initial margin vs ESMA stop-out 50%), ENTRY_OVERSHOT 0.5*ATR policy tolerance reclassification.

## PHASE 2 (2026-09-30 evening) — test speed, exit wiring, Brent/BTC, coverage analysis
- Start: runner on local d117016 (pid 22668 had died), broker DEMO flat, restarted (pid 13576). Direct delegation (no dispatcher): Opus read-only exit audit; Haiku scout test inventory; Sonnet builders = Lane T (test performance), E (exit wiring E1), M (Brent/BTC prep), N (market-first coverage analysis, offline). Codex unavailable (weekly quota exhausted until Oct 4) -> Sonnet took the engineering lane. Worktrees under phase2-worktrees/{t,e,m,n,m2}.
- Opus audit: ExitEngine was NOT wired in DEMO (only paper pipeline); DEMO exit = broker SL + fixed ~1.5R TP + forced flat; no partial/modify jobs; after a partial the SL child quantity was not resized; direction chain consistent, ROUND flip intentional (production_spec adds both mode complements). No Critical safety finding.
- Merged (no conflicts): T 79a384b (FAST 3218 tests 89 s / INTEGRATION 792 112 s / SAFETY overlay 2819 126 s / SLOW 605 634 s serial; expected FULL ~14 min, xdist opt-in; policy in CLAUDE.md/TEST_GATES.md/AGENTS.md: no full suite per change), M cabded1, E 2511480 (TakeProfitStage target_price/stage_id/source, ReduceJob/ModifyStopJob, SL/TP resize after partial, StagedExitManager, exit_policy default fixed_1_5r), N 75cd562 (coverage analysis: 3266 moves, near-miss/base-rate control; no enrichment over base rate under that definition, NOT an expectancy statement).
- INCIDENT 20:04 UTC: runner failed closed `clock_anomaly: server skew 305s` (flat, no exposure). Root cause: server time was the FIRST market's tick time, frozen by a paused market. Fix c1e01f2 (user-approved design): freshest tick across markets; clock reference only while it advances (clock_reference_window_s), abs skew both directions then; otherwise CLOCK_REFERENCE_UNAVAILABLE and stale-feed/session logic governs; tick far ahead of the local clock still fatal. Tests A-G green. Also isolation-test exemptions for offline src/coverage_analysis and markets/phase2.py.
- Checks after merge+fix: targeted 1653 passed, FAST 3309 passed, SAFETY 2911 passed, ruff + compileall clean. No full suite run.
- Live probe (runner paused, flat): `Brent` (BRENT CRUDE OIL SPOT) RED only for quote_fresh during the daily break (re-verify after 00:00 UTC); `BTCUSD` GREEN (min-lot margin ~370 EUR, spread median ~47-100 USD, 24/7 except Fri 22:55 -> Sat 09:00 server clock). Runner restarted on f7fb758 (pid 23116), RECONCILED, clock_reference OK. Lane M2 (Sonnet builder) wires both markets into live.py/production spec; both stay enabled=false until the lead flips them.
- Open: staged exit activation needs store censoring fix for engine exits (MANUAL), chart-derived TP/SL producer (lane E2), Gemini adversarial review, fast/microstructure data check, OUT_OF_WINDOW shadow outcomes (63% of analysed moves).
- 2026-09-30 Lane M2 (Sonnet builder, worktree phase2-worktrees/m2, branch phase2/lane-m2, base f7fb758): Brent + BTCUSD wired into the DEMO trader (code + tests only, both flags stay false): per-market enablement + per-market start-up preflight (disable-alone, heartbeat disabled_markets), production spec v1.1 superset (ORB only; v1 untouched), probe-observed spreads/leverage replace placeholders, BTCUSD calendar flat moved to 20:30 UTC. See docs/DEMO_TRADER.md + docs/V2_MARKETS.md 'Lane M2'.
