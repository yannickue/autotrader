# Data Agent Report

## Delivered

- Public Binance USD-M perpetual discovery from `exchangeInfo`, with dependency-injected JSON
  reading for deterministic tests and no credentials.
- Exact `Decimal` parsing of tick size, limit/market quantity steps, minimum quantity, and
  minimum notional, plus floor-to-increment and minimum-order helpers.
- Binance book-ticker and 24-hour-ticker normalization into `MarketSnapshot`, including event and
  receipt timestamps, bid/ask/last, derived spread and spread bps, volume semantics, quote-volume
  liquidity, volatility, sequence metadata, and configurable live/delayed/stale classification.
- Decimal-only movement and realized log-return volatility features.
- Configurable 20–100 market universe filtering and normalized weighted ranking across quote
  volume, spread, realized volatility, movement, liquidity, minimum notional, and venue status.
  Selection fails closed when fewer than the requested number of markets qualify.
- Deterministic unit fixtures and an explicit real-network integration test that is skipped by
  default.

## TDD Evidence

- Initial RED: owned tests failed during collection because `data.binance_usdm`,
  `features.market`, and `universe.selector` did not exist.
- First GREEN attempt: 14 passed and 2 failed, exposing an invalid successive-pair iteration and
  an over-specific equal-score ordering assertion.
- Focused GREEN after correction: 16 passed.

## Open Questions

1. The canonical cross-venue instrument identifier is not specified. This implementation uses the
   provisional existing convention `<venue-symbol>-PERP`, for example `BTCUSDT-PERP`, while always
   retaining the original Binance symbol.
2. Feed-specific staleness thresholds are not specified. They are configurable; defaults are one
   second for live-to-delayed and five seconds for delayed-to-stale.
3. The volatility window and annualization convention are not specified. The feature returns an
   unannualized square-root sum of log returns; the snapshot factory requires the caller's explicit
   window and records both that window and the method in metadata.
4. Liquidity methodology is not specified. Rolling 24-hour quote volume is used as the normalized
   snapshot liquidity proxy; depth-based liquidity can replace it without changing the boundary.
5. Sequence-gap recovery and duplicate suppression require state outside this stateless snapshot
   factory. Sequence identifiers are preserved in metadata for that future stateful adapter layer.

## Verification

- Owned unit tests: `16 passed in 0.17s`.
- Full suite: `23 passed, 1 skipped in 0.21s`; the skip is the explicit real Binance network test.
- Full Ruff gate: `All checks passed!`.
- `compileall` plus direct imports of the delivered modules: `compile/imports: ok`.
- The first direct-import check omitted the repository's `src` path and therefore failed with
  `ModuleNotFoundError: data`; confirming `src-on-default-path: False` isolated this to the check
  command. The corrected direct-import check explicitly added `src` and passed.

## Changed Files

- `src/data/binance_usdm.py`
- `src/data/models.py`
- `src/data/__init__.py`
- `src/features/market.py`
- `src/features/__init__.py`
- `src/universe/selector.py`
- `src/universe/__init__.py`
- `tests/unit/data/test_binance_usdm.py`
- `tests/unit/data/test_features.py`
- `tests/unit/data/test_universe.py`
- `tests/integration/data/test_binance_usdm_network.py`
- `DATA_AGENT_REPORT.md`
