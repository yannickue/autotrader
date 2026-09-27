# Strategy Agent Report — Sprint 1

## Scope delivered

- Added exactly three deterministic, configurable strategy families: Momentum, Breakout, and
  Pullback.
- Every family accepts a sequence of `MarketSnapshot` values and returns `Signal | None`.
- Every emitted signal contains instrument, LONG/SHORT direction, UTC event timestamp,
  versionable `strategy_id`, quote-based entry zone, invalidation level, expected move,
  expected horizon, confidence, and versioned feature attribution metadata.
- Confidence is explicitly labelled `ranking_score_not_probability` and is never used for sizing,
  leverage, orders, risk, or execution.
- Added deterministic `SignalFusion.rank()` and `SignalFusion.select()` behavior with optional
  minimum-confidence filtering, a deterministic caller-supplied veto rule, and optional
  same-instrument direction-conflict vetoing.
- No LLM, order, risk, execution, broker, or venue-adapter dependency was introduced.

## Family semantics

- **Momentum:** compares the first and last prices in a configured lookback and emits when the
  absolute return reaches the configured threshold.
- **Breakout:** compares the latest price with buffered extrema from the configured preceding
  range.
- **Pullback:** identifies a configured move away from the range origin, then requires a configured
  retracement while price remains on the trend side of the origin.
- IDs are deterministic SHA-256-derived idempotency keys over strategy, instrument, timestamp,
  direction, and feature attribution.

## TDD evidence

- Initial behavioral RED after public API skeleton: `11 failed, 4 passed`.
- Minimal family/fusion implementation: `1 failed, 14 passed`; the remaining failure was the
  intentionally unimplemented conflict-veto branch.
- After conflict-veto implementation: `15 passed`.
- Mutation check: temporarily weakened all three family thresholds; all three no-signal tests
  failed (`3 failed, 9 deselected`). Restored production thresholds and reran: `3 passed,
  9 deselected`.

## Final verification

Interpreter:
`C:\Users\yanni\.codex\.chatgpt-projects\g-p-6ab858dc4d7c8191818034e19b772625\trader\.venv\Scripts\python.exe`

- Owned tests: `python -m pytest tests/unit/strategies -q` → `15 passed in 0.07s`.
- Full suite: `python -m pytest -q` → `22 passed in 0.34s`.
- Ruff: `python -m ruff check src/signals src/strategies tests/unit/strategies` →
  `All checks passed!`.
- Compile: `python -m compileall -q src/signals src/strategies tests/unit/strategies` → exit `0`.
- Public imports with `PYTHONPATH=src` → `imports-ok`, exit `0`.

## Changed files

- `src/strategies/__init__.py`
- `src/strategies/_shared.py`
- `src/strategies/breakout.py`
- `src/strategies/fusion.py`
- `src/strategies/momentum.py`
- `src/strategies/pullback.py`
- `tests/unit/strategies/test_signal_fusion.py`
- `tests/unit/strategies/test_strategy_families.py`
- `STRATEGY_AGENT_REPORT.md`

## Integration decisions approved by lead

1. The data boundary has point-in-time `last` values but no canonical OHLC/bar or feature event.
   Breakout and Pullback use rolling `last` extrema for Sprint 1.
2. `expected_move` permits strategy-declared units but has no explicit units field. All three
   families use decimal return fractions for Sprint 1.
3. The architecture requires stale/invalid data to fail closed, but ownership at the strategy
   boundary is not explicit. These strategies enforce UTC timestamps and homogeneous instruments;
   freshness and `DataQuality` gating remain with data/risk integration.

## Open questions

None for this Sprint 1 scope.
