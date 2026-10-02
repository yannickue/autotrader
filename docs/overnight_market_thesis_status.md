# Overnight market-thesis programme — status ledger

Maintained continuously. Newest reality overrides older entries.

- CURRENT_PHASE: PHASE 4-6 (thesis stack committed fb50467; CODEX review of thesis + builder for position-thesis coverage and Workbench integration running) [updated 02.10. ~04:30]
- LAST_GREEN_COMMIT: 831a8dd (research/workbench-v1)
- OVERNIGHT_SCOPE_DONE: FALSE
- RUNNER_START_ALLOWED: FALSE (needs OVERNIGHT_SCOPE_DONE AND PRODUCTION_PREFLIGHT=GREEN AND time >= 08:30 Europe/Berlin)
- PRODUCTION: sprint1/integration @ 11c6aec, tree clean, STOP set, runner OFF; no research merge
- WORKBENCH_STATUS: all builder lanes A/B/C/D/F committed; T3: FAST TIMEOUT_INCOMPLETE (600 s, ~78 %, no failure observed), INTEGRATION 1122 passed/1 skipped, SLOW 830 passed/2 skipped, SAFETY running; final CODEX-0 pending
- COVERAGE_STATUS: implemented + CODEX-1 findings (3 High, 1 Medium, 1 Low) fixed in 5350ac9; position-thesis section pending
- THESIS_STATUS: MarketMap, main thesis, setup engine, CONTINUATION_RETEST committed (fb50467, 243 tests); CODEX implementation review running
- POSITION_MONITOR_STATUS: implemented (states, exit!=reverse, variants A-D), committed fb50467; review running
- TEST_STATUS: see WORKBENCH_STATUS
- CODEX_STATUS: 12 rounds, all Critical/High closed; CODEX-0 (post-fix verification of DAG compensation / lock fixes) pending
- DEPLOY_GATE: REVOKED_FOR_OVERNIGHT_WORK (explicit user authorization 03:25; executed `deploy_gate.py --revoke`; verified: `--check` rc=30 NOT_APPROVED, `deploy_approved.json` absent; production HEAD 11c6aec, tree clean, STOP present)
- RUNNER: OFF
- REMOTE_08_30_START: FAIL_CLOSED_BY_DEPLOY_GATE (remote trigger tools remain unavailable here, so the trigger itself is NOT_VERIFIED; the gate makes `run_trader_day.ps1` refuse with exit 30 even if STOP is removed)
- DEPLOY_GATE RESTORE RULE: only after OVERNIGHT_SCOPE_DONE=TRUE, all mandatory Codex gates closed, no Critical/High open, research worktree clean/checkpointed, no heavy research/builder/benchmark processes, RAM freed, production exactly 11c6aec and clean, no research commit merged into production, FRESH production preflight GREEN right before start, ActivTrades DEMO confirmed, reconciliation GREEN, no unknown positions/orders, local time >= 08:30 Europe/Berlin; restore via the existing approve_deploy mechanism (verify the current canonical mechanism first, do not invent alternatives). If the 08:30 trigger already ran and failed closed it will NOT retry: after restoring the gate do a fresh preflight, check no runner/supervisor is running, then use only the documented start mechanism; never double-start.

## DONE
Workbench v1 build + reviews (see docs/research_workbench_v1_report.md draft), benchmark (cold 17.5 s / warm 0.53 s / 5-15x invariants / resume per market / determinism PASS).

## ACTIVE
CODEX-0; Coverage inventory scouts.

## BLOCKED
none (runner start gate: user decision on the 08:30 trigger).

## KNOWN_FAILURES
T3 FAST segment timed out at 600 s under heavy machine load (not a test failure); re-run on a quiet machine pending.

## NEXT_ACTION
Finish T3 (safety), repeat FAST when quiet, CODEX-0, commit final workbench report, then Coverage Auditor.
