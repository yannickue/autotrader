# Deterministic research foundation

This package is offline-only. Production execution code must not import it.

- `storage.py` writes immutable, versioned Parquet partitions with a SHA-256 manifest. Events are
  replayed by recorded availability time; conflicting duplicate source keys fail closed.
- `replay.py` emits canonical dataset/configuration hashes for reproducibility.
- `backtest.py` exposes only observations available at the current simulated decision time and
  requires every result to be explicitly labeled `is` or `oos`.
- `walk_forward.py` creates disjoint, time-ordered rolling or anchored IS/OOS windows.
- `preparation.py` records a seeded OOS optimization plan and prepares column-oriented data without
  importing optional VectorBT or Optuna packages.

Trade metrics are implemented in `src/monitoring/metrics.py`. Sharpe and Sortino are per-trade,
non-annualized ratios because the current contracts do not define a return sampling frequency.

Runnable entry points:

```powershell
python scripts/backtest.py --trades trades.jsonl --initial-equity 10000 --split oos
python scripts/replay.py --root data --venue venue-a --instrument BTCUSDT-PERP `
  --event-type quote --date 2026-01-01 --dataset-version v1 `
  --configuration '{"latency_model":"recorded"}'
```

Both commands emit one compact, sorted JSON object suitable for capture by CI or experiment
tracking.
