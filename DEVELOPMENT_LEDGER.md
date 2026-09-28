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
