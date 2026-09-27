# Sprint 1 Final Report

Paper-only. Live order submission remains disabled (`configs/live.yaml`: `order_submission_enabled: false`,
`real_order_submission_enabled: false`, `live_credentials_allowed: false`).

## Version

- Branch `sprint1/integration` (mirrored to `claude/project-thread-9qktly`), tag `sprint1-rc1`
  (release candidate; final tag after the independent Codex review).
- Baseline: `bootstrap-v0.1` (9139dd1).

## Merged branches (in order)

1. `sprint1/data` — Binance USD-M data pipeline, features, universe selector
2. `sprint1/strategy` — deterministic momentum, breakout, pullback, signal fusion
3. `sprint1/research` — Parquet storage, replay, backtest, walk-forward, robust metrics
4. `sprint1/risk` — fail-closed risk engine
5. `sprint1/execution` — paper execution engine and portfolio ledger
6. Integration — paper pipeline and E2E scenarios (`src/pipeline`, `tests/integration`)

All uncommitted Codex work found at takeover was preserved verbatim before any change.

## Test results (integrated tree)

| Area | Tests |
|---|---|
| Data (+ live-network test, intentionally skipped) | 16 passed, 1 skipped |
| Strategy | 15 passed |
| Research + replay | 47 passed |
| Risk + property invariants | 73 passed |
| Execution + portfolio + chaos | 83 passed |
| E2E paper path | 15 passed |
| Contract types + imports | 7 passed |
| **Total** | **256 passed, 1 skipped** |

`ruff check .` clean · `compileall -q src scripts tests` clean · all module imports OK.

## E2E results (`reports/sprint1_e2e_evidence.json`, 15/15 PASS)

Full path: MarketSnapshot → Universe → Strategy → Signal → RiskDecision → ExecutionRequest → paper fill → Portfolio → Metrics.

| # | Scenario | Evidence |
|---|---|---|
| 1 | Accepted LONG | one position, qty 183.277 > 0, filled |
| 2 | Accepted SHORT | one position, qty −194.408, filled |
| 3 | Risk REJECT | `SPREAD_TOO_WIDE`, execution not called, qty 0, gross 0 |
| 4 | HALT | kill switch, `risk_engine.halt()`, execution HALTED, direct submit while HALTED → all zero new exposure |
| 5 | Stale signal | `SIGNAL_STALE`, qty 0, gross 0 |
| 6 | Duplicate request | reprocess + resubmit → qty unchanged (183.277) |
| 7 | Duplicate fill | duplicate trade event and duplicate `report_fill` → qty stays 1 |
| + | Max leverage | binding `leverage_cap`: decision leverage 20, account leverage 20; next signal rejected; `RiskPolicy(max_leverage=25)` raises |
| + | NaN/inf | `INVALID_INPUT`, qty 0; `MarketSnapshot` rejects NaN at construction |
| + | Invalid stop | stop == entry and wrong-side stop → `INVALID_STOP` |
| + | Unknown account | `ACCOUNT_UNKNOWN`, qty 0 |
| + | Risk bypass | None / rejected decision → `RISK_NOT_APPROVED`; id or side mismatch → `RISK_MISMATCH`; oversize → `EXCEEDS_APPROVED_SIZE`; qty 0 |
| + | Gross cap | second LONG capped: gross 1049.9 ≤ 1050 (exact bound, marked exposure) |
| + | Marked exposure | after open, gross ≥ qty × latest price |
| + | Metrics | open + close → finite, JSON-serializable (`allow_nan=False`) |

Defects found in review and fixed before merge: canceled/expired orders still matching trades; reduce-only
exits able to flip positions; execution accepting a decision with a different side or reduce-only flag;
unmarked positions counting as zero exposure (fail-open); unvalidated risk-policy numbers.

## Remaining stubs

- `src/adapters` empty; no venue adapter; NautilusTrader Sandbox not integrated (OPEN_QUESTIONS 21).
- Paper fills carry zero fees; slippage is a fixed bps model; no funding in paper PnL.
- Trailing stops: fixed-distance policy only. LIMIT IOC/FOK is canceled at submit (no trade event to cross).
- `src/features`: movement/realized volatility only. `src/monitoring`: metrics only (no alerts).
- No exit engine: strategies emit entries only; pipeline has no position-aware add/reduce-to-flat logic.

## Known limitations

- Risk reservations and execution checkpoints are in-process only; no durable journal or crash
  durability (OPEN_QUESTIONS 15, 22).
- Leverage = notional / equity (OPEN_QUESTIONS 12). No liquidation buffer or auto-deleverage:
  adverse price moves after entry can raise account leverage above the cap until the next decision.
- Daily-loss reset boundary not authoritative (OPEN_QUESTIONS 13); DELAYED data never opens exposure (17).
- Research metrics are per-trade and non-annualized; no walk-forward run on real recorded data yet;
  universe symbol subset still undecided (OPEN_QUESTIONS 1).
- Host environment: the uv-managed Python 3.12 is broken; gates run with `trader/.venv` (3.12.14).

## CODEX_REVIEW_PENDING

Scheduled for 2026-09-27 19:05, with diffs and contracts only (no full-repo reread):

1. Risk engine diff (`src/risk`, `tests/unit/risk`, `tests/property`)
2. Execution + portfolio diff (`src/execution`, `src/portfolio`, `tests/unit/execution`, `tests/unit/portfolio`, `tests/chaos`)
3. Integration diff (`src/pipeline`, `tests/integration`)

## Next recommended quantitative milestone

**Opportunity Scanner on recorded Binance USD-M data with a realistic cost model.** Record public
market data; add a fee/funding model to paper fills; build inspectable per-feature scores (liquidity,
spread, relative volume, volume acceleration, realized-volatility expansion, momentum, relative
strength, funding, open interest) without collapsing them into a single confidence score. Evaluate
with walk-forward, out-of-sample replay through the same Risk and Execution path. Prerequisite:
Codex review findings resolved.
