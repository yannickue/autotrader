# Pending User Input

All credential-free ActivTrades/MT5 foundation work is done: adapter, InstrumentSpec,
symbol discovery, data ingestion, and the four scripts below are real and wired together.
Nothing below requires typing secrets into chat or committing them anywhere -- set them
locally per `.env.example`.

## What only you can do

1. **Open ActivTrades MT5 DEMO and log in** via the MetaTrader5 terminal on this machine.
2. **Set these locally** (e.g. in a `.env` file at the repo root -- `.env` is git-ignored,
   `.env.example` shows the shape). Never paste real values into chat or commit them:
   ```
   MT5_LOGIN=<your demo account number>
   MT5_PASSWORD=<your demo account password>
   MT5_SERVER=<the ActivTrades demo server name shown in the terminal>
   MT5_TERMINAL_PATH=<only if the terminal isn't in its default install location>
   ```
3. **Run the preflight check**:
   ```
   uv run python scripts/mt5_preflight.py
   ```
   This runs config -> terminal connection -> account state (refuses a non-demo account) ->
   symbol availability -> a dry-run `order_check` (never sends a real order), and prints
   PASS/WARN/FAIL for each. It does not require any code changes from you.
4. **If `symbol_availability` reports WARN/UNVERIFIED instruments**, run:
   ```
   uv run python scripts/mt5_symbol_discovery.py
   ```
   and compare its table against what ActivTrades actually calls DAX/NASDAQ100/WTI in your
   terminal's Market Watch. The alias lists in `scripts/mt5_symbol_discovery.py` and
   `scripts/mt5_preflight.py` (`CANONICAL_INSTRUMENTS`/`_CANONICAL_INSTRUMENTS`) are
   illustrative placeholders, not confirmed real ActivTrades symbols -- correct them there
   (or pass explicit `overrides` to `match_symbols()`) once you know the real broker symbols.
5. Optionally run `scripts/mt5_connection_check.py` / `scripts/mt5_account_snapshot.py` for
   quicker, narrower checks than the full preflight.

## If preflight fails

Send back only the printed PASS/WARN/FAIL report (never a password) -- it names the exact
failing check and reason.

## What's already done (no action needed)

- `src/adapters/activtrades_mt5/`: typed read-side wrappers, connection/health state machine
  (`DISCONNECTED`/`CONNECTING`/`CONNECTED`/`DEGRADED`/`RECONCILING`/`READY`/`ERROR`,
  `CONNECTED != READY`), a guarded write-side interface (`TradingMode.MOCK` by default, never
  sends a real order except in `DEMO`/`LIVE_SMOKE`/`LIVE` modes with explicit confirmation).
- `src/instruments/`: canonical `InstrumentSpec` + deterministic symbol discovery/matching.
- `src/data/`: provenance, Parquet storage, data-quality checks.
- `src/costs/`: hardened so a cost schedule built for one venue/asset class can never silently
  price a different one (`ValueError` on mismatch); `make_cfd_cost_schedule()` for CFDs.
- Two real bugs found and fixed in unrelated Phase A code along the way (see
  `DEVELOPMENT_LEDGER.md`): a cross-instrument PnL leak, and a restart that silently resumed
  `READY` instead of `RECONCILING`.

## Not yet built (needs a real connection to develop against, or comes later)

- Live/historical bar and tick ingestion actually calling the adapter (the Parquet
  writer/reader exist; nothing populates it from MT5 yet).
- Comparison tests: internal margin/profit math vs. real `order_calc_margin`/`order_calc_profit`.
- FeatureRegistry, OpportunityScanner, Regime Engine, strategy adaptation to CFD features,
  VectorBT/Nautilus research pipeline, Shadow mode, Demo order execution, Live-Smoke.
