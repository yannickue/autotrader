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

## Phase 2 (Lane M): BRENT (ENERGY) and BTCUSD (CRYPTO), ActivTrades DEMO

Status: integrated and tested, **disabled**. Live probe BLOCKED (the running DEMO runner permanently holds the global MT5 lock,
`MT5_CONNECTION_BUSY`, 4 bounded attempts x 2 runs, nothing forced); preflight is **RED (UNVERIFIED)** for both markets.

Observed symbols (symbols_get, 523 symbols, snapshot 2026-09-30; `docs/evidence/phase2_symbol_probe.json`): `Brent`
("BRENT CRUDE OIL SPOT", `Spot Energy\Brent`; NOT `BrentDec26/BrentNov26` dated futures CFDs, NOT `LCrude` = WTI) and `BTCUSD`
("Bitcoin vs US Dollar", `Cryptocurrency\BTCUSD`; `BCHUSD` = Bitcoin Cash). Neither was in Market Watch (`visible=false`): no quote,
margin, rates or session data exist yet. Observed static facts: Brent point/tick 0.01, contract 1000 bbl, tick value 10 USD/lot,
lots 0.01/0.01/10, stops_level 5 pts, freeze 0, calc mode 4 (CFD leverage), swap mode 1 (points) long +9.536 / short -15.252;
BTCUSD point/tick 0.01, contract 1 BTC, lots 0.01/0.01/3, stops/freeze 0, calc mode 2 (CFD), swap mode 5 long -21 / short +3;
both USD profit/margin, trade_mode 4 (full).

Design
- `configs/markets_phase2/{BRENT,BTCUSD}.toml` (`MarketSpec` + `[cost]` + `[preflight]`), loaded by `markets.phase2`. They are NOT in
  `markets.spec.CANONICALS` (all research/opportunity code iterates it and needs history + strategy lists); the five existing
  markets and `configs/markets/` are untouched (bit-identical, golden-hash test).
- Clusters: `demo.execution.risk_policy.PHASE2_CLUSTERS` (BRENT->ENERGY, BTCUSD->CRYPTO); `cluster_of` resolves via `ALL_CLUSTERS`.
  `CLUSTERS` is unchanged on purpose: `Mt5DemoStack` start-up iterates it and fails closed for unregistered markets. ENERGY/CRYPTO use the
  same `RiskCaps.max_cluster_stop_risk_fraction` / aggregate / leverage (<=30x) caps as every other cluster; no one-position rule.
- Registry: `nautilus_mt5.symbols.BRENT/BTCUSD`, `phase2_registry()`, `demo_registry(extra_markets=())` (default = the five markets).
  DEMO facts: `markets.phase2.demo_market_specs(names)`.
- Calendars are PROVISIONAL. BTCUSD has no 24/7 assumption (provisional liquid-hours entry window; weekend UNVERIFIED until observed).
  Spread caps, reference median spreads and `max_leverage` (Brent 10x, BTC 2x: conservative assumptions, not observed) are placeholders
  the probe must replace; preflight fails if `max_leverage` exceeds the broker-implied leverage.
- Enablement: `configs/markets_phase2/enablement.toml` (`enabled=false`); `phase2.enabled_market_names(verdicts)` requires flag AND GREEN.

Preflight (`markets.preflight.run_preflight`, pure): account DEMO, exact symbol mapped, tradable, fresh quote, contract/tick/volume facts,
margin calc (implied leverage vs spec and the 30x cap), structural SL vs stops_level (no stop is tightened), protection path (SL order mode,
filling modes), persistence/registry/cluster wiring, observed session coverage, cost model from the observed spread (+ min-lot feasibility),
no market-name literals in generic code. GREEN only if all PASS; UNVERIFIED counts as RED.

Run (when the MT5 lock is free; attach-only, read-only, calc-only margin/profit):
1. `uv run python scripts/phase2_symbol_probe.py probe Brent,BTCUSD` (writes `docs/evidence/phase2_symbol_probe.json`; needs the symbols in
   Market Watch: the stack's `symbol_select` does this, the probe never calls it)
2. `uv run python scripts/phase2_symbol_probe.py verdict` (writes `docs/evidence/phase2_preflight_verdict.json`).

## Lane M2: Brent + BTCUSD wired into the DEMO trader (2026-09-30)

Facts and sources: all numbers below are OBSERVED in `docs/evidence/phase2_symbol_probe.json` (probe 2026-09-30 21:04 UTC, attach-only,
read-only) and `docs/evidence/phase2_preflight_verdict.json` (BTCUSD GREEN; BRENT RED only on `quote_fresh`: stale during its daily break).
Placeholders replaced (configs carry the provenance in `spread_model_notes` / `margin_notes` / `[cost]`):

| item | former placeholder | observed value used |
|---|---|---|
| BRENT spread cap `max_entry_spread_price` | 0.10 | 0.08 USD (= M1/M5 observed max 8 pts; M1 median 6, p95 6, p99 6) |
| BRENT reference median spread | 0.05 | 0.06 USD (M1 recorded median, 5000 bars) |
| BRENT `max_leverage` | 10 (assumed) | 10 (implied 9.99x, min lot 86.32 EUR at 97.78) |
| BTCUSD spread cap | 100 | 100 USD kept, now validated (M1 p99 71.65, max 104.7; M5 p99 66.0) |
| BTCUSD reference median spread | 40.0 | 59.83 USD (M1 recorded median; M5 median 47.25) |
| BTCUSD `max_leverage` | 2 (assumed) | 2 (implied 2.00x, min lot 369.69 EUR at 83746.28) |
| BTCUSD calendar (BOOTSTRAP / SAFETY SCHEDULE — not alpha-validated; must not become an undocumented permanent rule) | entry 08:00-20:00, flat 21:55 UTC | entry 08:00-19:30, flat/cash close 20:30 UTC (Friday break 22:55 server = 20:55 UTC) |

PROVISIONAL (not broker-confirmed): both calendars, the server clock = Europe/Berlin inference (observed +2 h at the probe), commission
(assumed 0), swaps (not modelled, forced flat), the ROUND-number scale entries for the new asset classes, and every strategy statement
(no edge claim; ORB class defaults only, see `docs/DEMO_TRADER.md` Lane M2 for the gaps).
Enable/disable: `configs/markets_phase2/enablement.toml` per market + runner restart; the start-up preflight then gates each market alone.
Code: `markets.spec.load_market_spec` resolves Phase-2 names to `configs/markets_phase2` by default, `markets.phase2.flag_enabled_markets`,
`markets.preflight.run_live_preflight`, `demo.opportunity.production_spec.load_production_spec_for` (v1 unless a Phase-2 market is enabled),
`Mt5DemoStack(extra_markets=...)` / `disabled_markets`, `demo.runner.build_live_runner(phase2_markets=...)`.

## Lane F: BTCUSD + Brent Discovery-family readiness (2026-10-01)
Status tag for everything below and for every Phase-2 snapshot: **PHASE2_DISCOVERY / NOT_ALPHA_VALIDATED**. No expectancy is claimed.

**Data (read-only, bounded MT5 worker, `scripts/v2_download_market.py BTCUSD,BRENT 2025-06 2026-09 "M5,M1@2026-06"`, output kept outside `data/`, uncommitted).**
BTCUSD M5: 83 187 bars, 2025-09-24 10:45 UTC .. 2026-09-30; months 2025-06..08 NO_DATA (terminal history starts Sep 2025); **2025-10 and 2026-03 REJECTED**
(`AmbiguousServerTime`: a 24/7 instrument has bars inside the Berlin DST fold hour, the UTC policy refuses to guess), two missing months. BTCUSD M1: 100 000 rows
(Jul-Sep 2026, terminal cap). Brent M5: 88 082 bars 2025-06-01 .. 2026-09-30, no rejected months; M1 100 000 rows (Jun-Sep 2026). Quality JSONs:
`research/reports/v2_markets/{BTCUSD,BRENT}_quality.json`. Research frames are cut at the dev end (`dev_frame`, no bar after the Berlin date 2026-08-31; September 2026 is
never loaded). These markets have **no frozen Train/holdout split**: nothing is fitted, selected or promoted from this history.

**Family `STRUCT` (`src/alpha/families/structbrk.py`).** Session-agnostic range-structure breakout: prior `n_range` (24) bars, compressed
(`MIN_WIDTH_SQRT 0.25 <= width/(ATR*sqrt(n)) <= COMP_MAX_SQRT 0.8`), break = bar CLOSE beyond the range with a volatility expansion (`TR >= 1.0 * prior ATR`),
structural stop = opposite edge -/+ 0.25 ATR (fade: failed-excursion extreme), fixed 1.5 R target, 12-bar cooldown. Variants (one module, parameter `mode`):
breakout / confirmed / retest / fade. **Gap-aware**: the range and the ATR window must lie inside one contiguous 5-minute segment
(`k - seg_start >= max(n_range, 15)`), so no range is built across the Brent daily break (20:55-00:00 UTC in CEST), the weekend or any missing bar. **Session-agnostic**:
no `cash_open`/`cash_close` is read; only the operating-policy entry window (`entry_mask`, Lane P) constrains entries. All constants are
**DISCOVERY PLACEHOLDERS** (random-walk scaling, reuse of ORB/VOLREV values, chosen before any BTC/Brent history was looked at; `structbrk.constants()`;
`CONSTANTS_VERSION` is part of every spec hash). `structure_levels` (range high/low, width in ATR, last confirmed swing high/low, break offset) is emitted
additively in `snapshot.signal["structure_levels"]` for the E2 exit-plan producer.

**Spec v1.2** (`production_spec_v1_2.json`, hash `a4fe51b558d03274`, strict superset of v1.1; v1 `c3eae99e782888ac` and v1.1 `4f4b33e97966cd84` untouched and loadable;
the five core markets' entries and provenance are byte-identical). BTCUSD and BRENT: STRUCT `confirmed` = PRIMARY (fixed a priori, before measurement),
`breakout`/`retest`/`fade` = SHADOW; ORB DROPPED for both (BTC `cash_open 08:00 UTC` is an invented open; Brent 08:00 London is inherited from XAU/EUR with no evidence of
a defensible opening range for the ActivTrades Brent spot CFD, which has no session-open auction in the observed bars). `load_production_spec_for` returns v1.2 iff a Phase-2 market
is enabled (same gating as v1.1). Tags: every Phase-2 snapshot carries `signal.phase = PHASE2_DISCOVERY`, `signal.alpha_status = NOT_ALPHA_VALIDATED`, `signal.role`; outcomes
and counterfactual labels join the snapshot by `opportunity_id`, so the tag is persisted with them (no schema change).

**Forward Shadow (implemented, small).** A SHADOW-role spec is evaluated and snapshotted like any spec, but an otherwise accepted decision is turned into the terminal
rejection `SHADOW_VARIANT` in `OpportunityEngine` (no intent, no broker order); the existing counterfactual labeller then produces MFE/MAE/R labels for it. The funnel
shows the code as UNCLASSIFIED/TRADABLE (engine-level code, like CATCHUP_*); confluence counts ignore SHADOW specs. Gap: labels use a hypothetical fill at the intended entry (no slippage).

**Offline variant measurement** (`scripts/lane_f_family_variants.py`, `docs/evidence/lane_f_family_variants.md/.json`, method lane-f-variants-v1): hindsight diagnostics, n per cell
and 'n too small' flags, random-bar base-rate control. Headline: mean R per variant is negative on both markets after spread (breakout/confirmed/retest roughly -0.05 to -0.18 R, fade
-0.18 to -0.26 R, hypothetical walk), the breakout trigger is statistically indistinguishable from random bars for a 3-ATR move, false-break rate within 6 bars is 56-61 %.

**Semantic readiness (causal, per market).**

| item | BTCUSD | BRENT |
|---|---|---|
| lookahead | none (truncation + future-perturbation tests, all 4 modes) | same |
| session semantics | none assumed; 24/7 structure; the Fri 20:55 -> Sat 07:00 UTC (summer) break is handled by contiguity | no open assumed; break 20:55-00:00 UTC (summer; 21:55-01:00 winter) handled by contiguity; calendar source = probe-derived bar sessions (PROVISIONAL, not broker-confirmed) |
| DST | server clock Europe/Berlin -> UTC policy; the DST fold hour (2026-10-25 server 02:00-03:00) is ambiguous: history download rejects it, live fetch raises `AmbiguousServerTime` (fail-closed, bar skipped) | fold hour lies inside the daily break: no bars, not affected |
| spread/price (recorded M5 median) | ~6 bps (45.7 USD); live tick spread ~100 USD | ~6 bps (0.05 USD) |
| spread/stop (median; structural stop ~4 ATR) | ~0.10 (16 % of candidates above the policy 20 %-of-1R gate; fade 56 %) | ~0.09 (10 %; fade 47 %) |
| movement_to_cost (median 4 h MFE / spread) | ~6 | ~8 |
| verdict | family semantics GREEN (session-agnostic, gap-aware, causal); cost/edge UNVALIDATED | family semantics GREEN with the PROVISIONAL Brent calendar caveat; cost/edge UNVALIDATED |
