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
| Risk | sprint1/risk | COMPLETE, MERGED; CODEX_REVIEW_PENDING | 80 passed |
| Execution | sprint1/execution | COMPLETE (paper only), MERGED; CODEX_REVIEW_PENDING | 90 passed |
| Integration | sprint1/integration | All modules + paper pipeline + E2E merged | 265 passed, 1 skipped; E2E 15/15 |

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

**NOT fixed this run — prioritized backlog, highest severity first:**

1. **[HIGH] Execution — cancel/replace drops protective-order linkage.** Replacing an entry order
   loses its future stop; replacing a stop order loses its trigger price (next matching trade
   raises `INTERNAL_ERROR` and halts, with the position already unprotected). Needs: carry
   stop/take-profit/OCO/trailing fields through cancel/replace, and a real "move stop" path (today
   `new_price` only edits the limit price).
2. **[MEDIUM] Execution — fills after a stop reaches a final state leave the position
   unprotected.** `_ensure_protective` never re-creates/resizes protection once the linked stop is
   terminal (e.g. reduce-only close cancels the stop, then a late partial fill on the original
   entry re-opens exposure with nothing guarding it).
3. **[MEDIUM] Execution — duplicate-trade dedup key omits the instrument**, so identical trade
   ids on two different instruments collide and the second trade is dropped as a duplicate
   (`src/execution/paper.py:385`).
4. **[MEDIUM] Execution — `import_checkpoint` restores straight into `READY`**, skipping
   `RECONCILING`, contradicting `EXECUTION_CONTRACT.md`'s explicit startup/reconnect requirement.
   The existing chaos test hides this by setting mode by hand after restore.
5. **[MEDIUM] Execution — pipeline slippage guard never actually rejects anything**
   (`src/pipeline/paper.py:239`): the reference price it compares against is derived from the same
   quote that sets the fill price, so the computed deviation always equals the fixed
   `slippage_bps` constant. Needs a real independent reference price.
6. **[OPEN QUESTION — do not silently fix] Risk — sizing/cap math uses `request.entry_price`
   throughout (leverage cap, gross/net capacity, instrument notional, liquidity), only comparing
   market bid/ask for the spread check.** A caller-supplied entry price far from the market
   (repro: SELL request `entry=10` against market `bid/ask=99/101`) is approved at a computed
   leverage far under cap while the *real* notional at actual fill price would exceed it. Whether
   the fix is "reject when entry deviates from the executable market price beyond a tolerance" or
   "size from `max(entry, executable_price)`" is a contract decision (tolerance value, reject vs.
   clamp) this project's own `docs/OPEN_QUESTIONS.md` preamble says must not be silently decided by
   whoever finds it — added as `OPEN_QUESTIONS` item 23 below instead of patched.
7. **[LOW]** Risk: reservation is stored before the decision object is built (engine.py ~468 vs.
   470-496); an exception in between would leak a reservation. `_reject` also isn't robust to a
   caller passing a non-enum `side`.
8. **[LOW]** Risk: Decimal rounding at the leverage re-check (engine.py ~459-465) can raise
    `RuntimeError` on an epsilon-level overshoot, halting the engine (denial-of-service) rather
    than returning a normal `EXPOSURE_LIMIT` rejection.
9. **[LOW, not a safety issue]** Execution: paper fills are optimistic — stops fill exactly at
    the trigger price even on a gap-through trade, and a single trade print's quantity can be
    consumed by multiple resting orders at once. Affects backtest/paper realism, not live safety
    (live submission stays disabled regardless).

Repro scripts for every fixed and unfixed HIGH/MEDIUM finding above are preserved at
`C:\Users\yanni\AppData\Local\Temp\claude\C--Users-yanni-OneDrive-Desktop-Claude-Wokstation\29edb70d-3dd4-4055-8de4-4e2cd3428abe\scratchpad\probe.py` and `probe2.py` (external session's
scratchpad — copy into this repo's `scripts/` if they need to survive that session's cleanup).

## Blockers

- Codex hit a second usage limit shortly after task G1; reset time unknown. Not a hard blocker —
  fixes proceed on Sonnet with Codex used opportunistically for review when available again. The
  originally-scheduled 19:05 full-diff review never ran; a single STRONGEST_AVAILABLE+HIGH
  independent review of the complete resulting diff is still owed once all three HIGH findings
  are fixed (see Next decision).
- uv-managed Python 3.12 install on this host is broken; tests run with `trader/.venv` (3.12.14).

## Next decision

HIGH Finding #1 (late fill exceeds approved size) and HIGH Finding #2 (protective stop/TP expiry
unprotected) are now DONE (tasks G1/G2, H above). One HIGH finding remains: backlog item 1
(cancel/replace drops protective-order linkage) — do this before any new feature work. One
finding at a time: reproduce → regression test → minimal fix → targeted tests → subsystem tests →
ledger update. Item 6 needs an explicit policy decision (see `docs/OPEN_QUESTIONS.md` #23) before
it can be coded — do not guess a tolerance value. Finding #1 got an independent Codex review
(task G1); Finding #2's fix has NOT (Codex was usage-limited again when it came up, per explicit
instruction not to wait on it) — get that review in before or alongside the final full-diff audit,
not skip it silently.

After all three HIGH findings are fixed: full pytest + ruff + compileall, then one final Codex
independent review of the complete resulting diff (STRONGEST_AVAILABLE = `gpt-6-astra` empirically
confirmed reachable on this account, effort=HIGH; escalate to XHIGH only for a concrete unresolved
critical conflict). Only after that review is clean should `sprint1-rc1` be tagged and the
"Opportunity Scanner" milestone from `SPRINT1_FINAL_REPORT.md` start. Live trading stays
hard-disabled regardless of test/review outcomes.
