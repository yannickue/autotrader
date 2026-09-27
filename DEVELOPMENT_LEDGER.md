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
| Integration | sprint1/integration | All modules + paper pipeline + E2E merged | 256 passed, 1 skipped; E2E 15/15 |

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
| E. Independent review of Risk/Execution/E2E diffs | Codex (scheduled 19:05, diffs + contracts only) | findings verified by tests | CODEX_REVIEW_PENDING |
| F. SPRINT1_FINAL_REPORT.md | Sonnet builder (from ledger + test output) | none | after D |

No Opus agent used; escalation criteria not met.

## Blockers

- Codex usage limit reached (resets 2026-09-27 19:02 local). Items marked `CODEX_REVIEW_PENDING`
  go to Codex when available: Risk engine review, Paper execution review, integration diff review.
- uv-managed Python 3.12 install on this host is broken; tests run with `trader/.venv` (3.12.14).

## Next decision

After A–C land: merge Research → Risk → Execution sequentially, run the full suite after each,
then write the E2E scenarios in `tests/integration/test_e2e_paper_path.py`.
