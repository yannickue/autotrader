# Pending User Input

## Status: MT5 connection is live and working

Confirmed on your desktop (2026-09-29): `uv run python scripts/mt5_preflight.py` now passes
config, terminal_connection, account_state (demo, EUR 500 equity), and order_check (dry-run,
Ger40/DAX, no real order sent). Python is pinned to 3.12, `.env` is set locally (git-ignored,
never committed/printed), and no MT5 call can hang the terminal (hard-timeout-bounded).

**WTI is not offered on this ActivTrades DEMO account** (confirmed by you) -- the earlier
`symbol_availability: WARN` for WTI is not a bug or a matching failure, the instrument simply
isn't in this account's tradeable symbol list. DAX and NASDAQ100 both resolved correctly.

WTI stays in the target instrument set for eventual real-account trading -- no code change is
needed for that: `src/instruments/discovery.py`'s canonical<->broker symbol matching is fully
generic (it works from whatever `symbols_get()` returns), so it will resolve WTI automatically
the moment it's run against an account whose symbol list actually includes it, with no changes
required here. Until then, the DAX+NASDAQ100 vertical slice is fully live-connection-verified and
research-ready on its own.

Real-money/live-account activation is a separate, explicit decision the user makes later (the
adapter's `TradingMode` enum already defaults to the safest mode and requires an explicit
confirmation flag for `LIVE`/`LIVE_SMOKE` -- see `src/adapters/activtrades_mt5/orders.py`); this
session keeps the architecture correct and ready for that, but does not enable it.

## What only you can do (ongoing)

Whenever something needs a live check, run it yourself directly on your own desktop (not through
an automation tool's terminal) -- this session's own shell processes cannot reliably reach the
MT5 terminal's IPC even when the terminal is running (a Windows desktop/window-station access
limitation for automation contexts, unrelated to the code).

```
uv run python scripts/mt5_preflight.py
```
or double-click `run_mt5_preflight.ps1` (or `.cmd`). Send back only the printed PASS/WARN/FAIL
report (never a password).

Other available diagnostic scripts (all read-only / dry-run, never send a real order):
- `scripts/mt5_connection_check.py`, `scripts/mt5_account_snapshot.py` -- quicker, narrower
  checks than the full preflight.
- `scripts/mt5_symbol_discovery.py` -- full canonical/broker symbol mapping table.
- `scripts/mt5_diag_*.py` -- temporary, narrowly-scoped diagnostics from the earlier connection
  troubleshooting; safe to ignore/delete once no longer needed.

## What's already done (no action needed)

- `src/adapters/activtrades_mt5/`: typed read-side wrappers, connection/health state machine,
  bounded IPC (subprocess + hard timeout, cannot hang), a guarded write-side interface
  (`TradingMode.MOCK` by default, never sends a real order except in `DEMO`/`LIVE_SMOKE`/`LIVE`
  modes with explicit confirmation). `order_check`'s real success convention (`retcode=0`, not
  `order_send`'s `TRADE_RETCODE_DONE`) verified against a live account and fixed.
- `src/instruments/`: canonical `InstrumentSpec` + deterministic symbol discovery/matching.
- `src/data/`: provenance, Parquet storage, data-quality checks.
- `src/costs/`: hardened so a cost schedule built for one venue/asset class can never silently
  price a different one; `make_cfd_cost_schedule()` for CFDs.
- Python 3.12 pinned deterministically (`.python-version`, narrowed `requires-python`); a fresh
  `uv sync` installs everything, including the Windows-only `metatrader5` dependency
  (`sys_platform == 'win32'` marker), automatically.

## Not yet built (next steps)

- Live/historical bar and tick ingestion actually calling the adapter (the Parquet
  writer/reader exist; nothing populates it from MT5 yet).
- Comparison tests: internal margin/profit math vs. real `order_calc_margin`/`order_calc_profit`.
- FeatureRegistry, OpportunityScanner, Regime Engine, strategy adaptation to CFD features,
  VectorBT/Nautilus research pipeline, Shadow mode, Demo order execution, Live-Smoke.
