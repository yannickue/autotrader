# V2 Markets (Lane C, Phase 6a)

Research-only market definitions and history for GER40, NAS100, SPX500, XAUUSD, EURUSD on the
ActivTrades MT5 DEMO terminal. Nothing here enables trading: `MarketSpec.trading_enabled` must be
`false` (validated), the new registry entries are `research_only`, and the live/paper
`default_registry()` is unchanged (GER40 only).

Artifacts
- `src/markets/spec.py` `MarketSpec`/`SessionCalendar` + TOML loader/validator (`configs/markets/<CANONICAL>.toml`).
- `src/markets/quality.py` completeness analysis (pure pandas).
- `scripts/v2_discover_symbols.py` -> `research/reports/v2_markets/symbol_snapshot.json`.
- `scripts/v2_download_market.py` -> `data/markets/` (git-ignored) + `research/reports/v2_markets/<CANONICAL>_quality.json`.
- `src/adapters/activtrades_mt5/history*.py`: H4 (`16388`) mapped, `resolve_broker_symbol(broker_symbol=, path_prefix=)`,
  `download_bars(broker_symbol=, path_prefix=)`; H4/D1 validated on the server-clock grid (stored `ts` stay true UTC).
- `src/nautilus_mt5/symbols.py`: `NAS100/SPX500/XAUUSD/EURUSD` mappings (`research_only=True`), `research_registry()`.

## Symbols resolved (observed via `symbols_get`, 523 symbols, 2026-09-30)

The alias matcher alone mis-ranks (NAS100 -> `UsaTecDec26` future, SPX500 -> `PLUS.UK` "Plus500 Ltd"), so a
symbol counts as resolved only if EXACTLY ONE non-dated symbol satisfies a path prefix plus a description regex
(`RESOLVE_RULES` in the discovery script). Otherwise it fails closed.

| Canonical | Broker symbol | Path | Description | point | contract | volume min/step/max | ccy |
|---|---|---|---|---|---|---|---|
| GER40 | `Ger40` | Cash Indices | DAX Cash Index | 0.01 | 1 | 0.25/0.25/250 | EUR |
| NAS100 | `UsaTec` | Cash Indices | US Tech 100 Cash Index | 0.01 | 1 | 0.2/0.2/200 | USD |
| SPX500 | `Usa500` | Cash Indices | SP 500 Cash Index | 0.01 | 1 | 0.5/0.5/500 | USD |
| XAUUSD | `GOLD` | Metals | Gold | 0.01 | 100 | 0.01/0.01/50 | USD |
| EURUSD | `EURUSD` | Forex\Majors | Euro vs US Dollar | 1e-5 | 100000 | 0.01/0.01/50 | USD (margin EUR) |

Account currency is EUR. Dated futures (`...Dec26`) are never used. Near-misses listed in the snapshot (not resolved,
listing only): `UsaInd` (Dow, ambiguous with `UsaIndDec26`), `GBPUSD`, `EURJPY`, `UK100`, `Brent`.
Not found: none of the five is missing. `symbol_info` exposes no session times (`session_deals` empty), so sessions are
derived from data (below), not from the broker's symbol sessions.

Observed leverage (read-only `order_calc_margin`, 1 lot): GER40/NAS100/SPX500 about 20x (5%), XAUUSD about 10x, EURUSD 30x.
`max_leverage` in the specs records the observed broker value; the 30x cap is a ceiling, not a target.

## Data downloaded (DEMO, read-only, `data/markets`, 19 MB total)

The MT5 terminal serves at most 100000 bars per timeframe ("max bars in chart"), so depth per timeframe is capped and
the M5/M1 window slides forward with time. Consequence: `data/ar1_ger40` (fetched earlier) starts 2025-02-04 while a
re-download starts 2025-02-10. Keep `data/ar1_ger40` as the frozen GER40 research set.

| Market | M5 (bars/first bar) | M1 (first bar) | H4 first | D1 first |
|---|---|---|---|---|
| GER40 | 100000 / 2025-02-10 | 2026-06-03 | 2017-02-13 (13702) | 2017-02-12 (2447) |
| NAS100 | 100000 / 2025-05-02 | 2026-06-19 | 2015-01-01 (15548) | 2013-04-08 (3527) |
| SPX500 | 100000 / 2025-05-02 | 2026-06-19 | 2017-04-02 (14658) | 2017-04-02 (2486) |
| XAUUSD | 100000 / 2025-05-05 | 2026-06-19 | 2015-01-02 (18180) | 2013-08-07 (3452) |
| EURUSD | 100541 / 2025-05-26 | 2026-06-24 | 2015-01-01 (18886) | 2005-01-01 (6779) |

M1 is 100000 bars per market (about 3.5 months); M1 for 2026-01..05 returned no data (NO_DATA, not an error). All last bars: 2026-09-30.
Run: `scripts/v2_download_market.py ALL 2025-01 2026-09 "M5@2025-01,M1@2026-01,H4@2015-01,D1@2005-01" data/markets`
(refuses if free disk < 2 GB or estimated total > 150 MB; re-runs of the still-open current month create a new
file name, use the manifest `data/markets/manifest_<CANONICAL>.json`, not the directory listing).

## Timestamp policy and DST cross-check

Same for every market and timeframe: MT5 `time` = server wall clock encoded as epoch; server = Europe/Berlin clock
(UTC+2 CEST / UTC+1 CET), converted by `ServerTimePolicy`; stored `ts` = bar-OPEN, true UTC. The rule is inferred
(live offset +7199 s), not broker-confirmed, but the data confirms it independently: the tick-volume peak sits at the
cash open and moves with DST in every market.

- GER40: peak 07:00 UTC in summer, 08:00 UTC in winter (= 09:00 Berlin, Xetra open).
- NAS100/SPX500/XAUUSD: peak 13:30 UTC (EDT) / 14:30 UTC (EST) (= 09:30 New York).
- EURUSD weekly open: 21:00 UTC (52 weeks, EDT) / 22:00 UTC (18 weeks, EST) = 17:00 New York.
- NAS100/SPX500/XAUUSD weekly open: 22:00 UTC (56 weeks) / 23:00 UTC (18 weeks).
- GER40 weekly open is a constant 00:15 UTC (82 of 86 weeks) and the daily close is fixed at 22:00 Berlin
  (20:00 UTC summer, 21:00 UTC winter): 237 M5 bars/day in summer, 249 in winter. This is broker schedule, not a
  policy error, but it means Berlin-minute buckets before about 02:15 differ between seasons.

## Quality findings (per-market JSON in `research/reports/v2_markets/`)

Common to all: 0 duplicate timestamps, strictly monotonic, 0 future timestamps, 0 off-grid bars, 0 inconsistent OHLC,
0 zero tick volume, 0 zero/negative spreads on M5/M1, every parquet passes the hash-verified round trip. No month was
REJECTED (only the empty 2025-01 requests are NO_DATA). DST-week check (Fri..Mon around every EU/US transition inside
the range): no market has a day below 60% of its profile.

| Market | M5 spread pts (median/p95/p99/max) | Missing weekdays (M5) | Intraday gaps > 3 bars | Notes |
|---|---|---|---|---|
| GER40 | 145 / 562 / 771 / 2177 | 11 (Easter, 1 May, Christmas, New Year) | 1 (2025-03-03, 45 min) | 166 SPREAD_ANOMALY bars |
| NAS100 | 75 / 134 / 188 / 446 | 2 (25 Dec, 1 Jan) | 17 | 65 min break 20:55-22:00 UTC for four days after the Oct 26 EU change |
| SPX500 | 49 / 161 / 192 / 240 | 2 | 17 | same pattern as NAS100 |
| XAUUSD | 30 / 35 / 47 / 500 | 3 (adds Good Friday) | 16 | daily 1 h break |
| EURUSD | 5 / 10 / 53 / 130 | 0 | 1 | Friday 276 M5 bars vs 288; 1066 SPREAD_ANOMALY bars (10x median rule, tiny median) |

Caveats for research use
- H4/D1 recorded spread is 0 for old bars on NAS100 (1054/1070 bars), XAUUSD (3912 H4 / 1039 D1), SPX500 (164/29): do not
  use H4/D1 spread for cost modelling.
- Volume is broker tick activity, OHLC is BID only.
- Validator `SUSPICIOUS_GAP` warnings on D1/H4 are weekend/holiday artefacts of the GER40-shaped gap rule, not defects.
- Calendars for NAS100/SPX500/XAUUSD/EURUSD (`calendar.status = "provisional"`) are structural proposals (US cash session
  09:30-16:00 New York; London 08:00-17:00 for gold/FX; entry window and forced flat derived from them) and are NOT tuned.
  Spread caps for those markets are provisional (observed M5 p99, in PRICE units); the cost model must use the recorded spread.

## Parameterisation checklist for the alpha worker

Replace GER40 constants by `load_market_spec(canonical)` (single source of truth). Units pitfall first:
`SimRules.max_entry_spread_pts` (8.0) is compared with `Frame.spread` = recorded points x `POINT`, i.e. it is PRICE units
(8.0 index points = 800 recorded points, about p99.5 of GER40), despite the `_pts` name. The spec exposes
`max_entry_spread_price` and `max_entry_spread_recorded_points`.

| File:line | Constant / assumption | Spec field |
|---|---|---|
| `src/alpha/common/dataset.py:19` | `POINT = 0.01` | `point_size` |
| `src/alpha/common/dataset.py:21` | `BERLIN = "Europe/Berlin"` | `calendar.tz` |
| `src/alpha/common/dataset.py:1,65,66,104` | GER40/M5/`Ger40` text, `DEMO` account-kind check in `load_research_dataset`; loader reads `<root>/download_manifest.json` (v2 manifests are `manifest_<CANONICAL>.json` with `entries[]`, `tf`, relative `path`) and `_admit` uses GER40-reviewed gaps `REVIEWED_GAPS` | per-market manifest + gap review |
| `src/alpha/common/frame.py:18-20` | `ENTRY_START_MIN`, `ENTRY_END_MIN`, `FLAT_MIN` | `calendar.entry_start_min/entry_end_min/forced_flat_min` |
| `src/alpha/common/frame.py:22-29,33` | `SESSION_BUCKETS`, `session_bucket()` | `calendar.bucket_tuples()` |
| `src/alpha/common/frame.py:15,58,71` | `BERLIN`/`POINT` imports, local-minute and spread scaling in `Frame.from_dataframe` | `calendar.tz`, `point_size` |
| `src/alpha/common/sim.py:28-30,146,211` | window/flat constants used in the entry filter and forced flat | calendar fields |
| `src/alpha/common/sim.py:44` | `commission_eur_per_lot` "GER40 demo: 0 observed" (not verified for other markets; contract sizes/currencies differ) | `contract_size`, `currency_profit` |
| `src/alpha/common/sim.py:83,152-153` | `max_entry_spread_pts = 8.0` (price units) | `max_entry_spread_price` |
| `src/alpha/fast/sim.py:16,506-509` | passes the same constants as explicit numba args | calendar fields |
| `src/alpha/timeframe/__init__.py:17,73,172`, `src/alpha/strategies/opening_drive/__init__.py:10,45` | `BERLIN` local-time conversions | `calendar.tz` |
| `src/alpha/strategies/breakout_retest/strategy.py:11` | `ENTRY_END_MIN = 16 * 60 + 30` (own constant) | calendar |
| `src/alpha/strategies/_common.py:83`, `_shared.py:62`, `src/alpha/fast/provider.py:79` | `instrument="GER40"` | `canonical` |
| strategy modules | `GER40_*_V1` names, docstrings referencing the Xetra session | naming |

Also note: bar counts per day vary by season (GER40 237/249), so any "bars since open" or day-length assumption must use
local-time minutes, not bar indices; `Frame.contig_next` gap handling already covers the daily breaks.

## Derived cost/sizing (per market, `src/alpha/common/market_costs.py`)

`CostScenario.slippage_pts` and `SizingSpec.min_risk_pts/max_risk_pts/contract_size` are PRICE units of the market, so
they cannot be shared between markets. `cost_scenarios_for(spec)` / `sizing_for(spec, account_eur=500)` derive them from the
MarketSpec plus the observed M5 median spread in `research/reports/v2_markets/<X>_quality.json`; GER40 reproduces
`COST_SCENARIOS` / `DEFAULT_SIZING` exactly (`sizing_for(GER40, account_eur=10_000) == DEFAULT_SIZING`).

- Slippage = fixed fraction of the market's median spread (V1 GER40: 0.5 / 1.5 over 1.45 = 0.345 / 1.034), rounded to the tick.
- Min risk distance = 3.448 median spreads (V1 5.0 / 1.45); max risk = 275.9 median spreads (V1 400 / 1.45), a sanity ceiling only.
- Lot min/step and contract size from the spec; `contract_size` = EUR per price unit per lot (USD profit converted with the constant
  reference EUR/USD implied by the 2026-09-30 NAS100 margin observation, 0.8818 EUR per USD).
- Leverage cap = min(10, broker-observed); the 30x ceiling is never a target. Risk-based sizing skips (`size_below_min`) when the
  minimum lot already risks more than `equity x risk_fraction` at the given stop, or when the leverage cap leaves less than one minimum lot.
- Sim window: `simulate_fast(..., window=SimWindow.from_spec(spec))`, minutes in the local calendar timezone of `Frame.minute`
  (DST handled by the frame's tz conversion); `window=None` = V1 GER40 constants.

Table for account 500 EUR, risk fraction 0.5%:

| Market | median spread | slip BASE/STRESS | min..max risk | lot min/step | EUR per price unit per lot | lev cap | min lot risk at min stop (EUR) | approved risk (EUR) | min lot leverage |
|---|---|---|---|---|---|---|---|---|---|
| GER40 | 1.45 | 0.5 / 1.5 | 5..400 | 0.25/0.25 | 1 | 10 | 1.25 | 2.5 | 12.7x |
| NAS100 | 0.75 | 0.26 / 0.78 | 2.59..206.9 | 0.2/0.2 | 0.88182 | 10 | 0.4568 | 2.5 | 10.7x |
| SPX500 | 0.49 | 0.17 / 0.51 | 1.69..135.17 | 0.5/0.5 | 0.88182 | 10 | 0.7451 | 2.5 | 6.77x |
| XAUUSD | 0.3 | 0.1 / 0.31 | 1.03..82.76 | 0.01/0.01 | 88.182 | 10 | 0.9083 | 2.5 | 7.35x |
| EURUSD | 5e-05 | 2e-05 / 5e-05 | 0.00017..0.01379 | 0.01/0.01 | 88182 | 10 | 0.1499 | 2.5 | 2x |

Reading the table: at 500 EUR the approved risk is 2.5 EUR and the 10x cap allows 5000 EUR notional. The minimum lot notional
(observed price, last column) exceeds that for GER40 (12.7x) and NAS100 (10.7x), so every index trade there is skipped as
`size_below_min` unless the account or the research leverage cap is raised (the broker-observed 20x and the 30x ceiling would allow it).

NOT modelled: commission (GER40 demo 0 observed, others unverified: 0 assumed), swap (no overnight), FX-rate variation (constant
rate), slippage measured from fills (calibrated fraction of spread), margin/stop-out mechanics, holiday/half-day early closes, `tick_value`.

## Unresolved / blocked

- Broker confirmation of the server-clock rule and of official session hours (cannot be read via MT5 `symbol_info`).
- 100000-bar terminal cap: longer M5/M1 history needs the terminal's "Max bars in chart" raised (a terminal setting, not
  changed here) or tick-level reconstruction.
- Provisional calendars/spread caps for the four new markets, and per-market commission/swap assumptions (swap
  long/short observed in the snapshot but not modelled; overnight holding is disallowed by the forced-flat rule).
- Free disk on C: was about 5-7 GB during this work (other processes also write there); output stayed at 19 MB.
