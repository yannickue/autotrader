# Gate C: controls-3 layout adapter, preflight and dry-run (2026-10-01)

Offline only. Counts and manifests; no fit/validate/oos run, no feature-vs-label distribution, no data >= 2026-09-01. Branch `observer/gate-c-run`
(= `observer/gate-c-prep` 7d7bfd4 + merge of `observer/lane-d3` a69c5b4). The preregistration
`docs/OBSERVER_GATE_C_PREREGISTRATION.md` (prereg-2) is byte-identical (SHA-256 `4413e35ea3fc7fe38131a3956875ec1402534f10ba55b711d199722300091110`
before and after; block hash `dc6d68cc332f5cc7...`).

## Schema reconciliation (adapter only, in `scripts/observer_gate_c.py`)

| Prereg-2 / script expects | Controls-3 builder writes | Adapter |
|---|---|---|
| `<M>/controls.parquet` | `<M>/controls3/controls.parquet` (joined table, same 134 columns as `table.parquet`) | `resolve_file`: `controls3/` is authoritative when it exists |
| `<M>/controls_b.parquet` | `<M>/controls3_b/controls.parquet` (same columns) | `resolve_file` |
| `<M>/controls_manifest.json` | `<M>/controls3/controls_manifest.json` (the flat one next to the events is the controls-2 manifest) | `resolve_file` |
| `<M>/controls_v2.parquet` (NC-C, explore) | flat `<M>/controls.parquet` = the controls-2 set | `resolve_file` fallback, only when `controls3/` exists |
| `balance_gate[P] = {match_rate, smd_local_minute, smd_atr_pct, smd_spread_pct, censoring_diff_pp, passed, status, n_controls}` | `balance_gate = {gate_version, thresholds, partitions: {P: {match_rate, smd{...}, censored_share{diff}, verdict, n_controls}}, market_status}` | `normalize_balance_gate`: pure relabelling; `censoring_diff_pp = 100 * censored_share.diff`; `passed = (verdict == PASS)` |

Safeguards: the builder thresholds (0.90 / 0.10 / 0.05) must equal the preregistered ones, otherwise the market is refused (structural defect, no
silent bending). The script still recomputes the gate under the preregistered thresholds and requires `passed` to agree. The builder has one extra
criterion (`session_share_diff_max` 0.02) that the preregistration does not have: if it alone fails a market, the builder verdict (FAIL) and the
recomputation (pass) disagree and preflight reports a structural error rather than choosing one. This did not occur in the real data. The old flat
layout keeps working (existing tests unchanged). Thresholds, hypotheses, statistics and prereg text were not touched.

## Preflight on the real files (`%LOCALAPPDATA%\Temp\observer_backfill`, stage fit = TRAIN)

All seven markets: `control_method_version = observer-controls-3`, structurally OK, no errors.

| Market | Scope | Balance (TRAIN) | Events / controls (TRAIN) | Fingerprint | Controls B |
|---|---|---|---|---|---|
| GER40 | core | descriptive_only (match 0.844) | 5929 / 5004 | f52ffd3fc457 | yes |
| NAS100 | core | descriptive_only (match 0.762) | 3200 / 2440 | 98103c9ca5b5 | yes |
| SPX500 | core | descriptive_only (match 0.725) | 1843 / 1337 | 3ce707648095 | yes |
| XAUUSD | core | descriptive_only (match 0.843) | 4255 / 3588 | b02905253a73 | yes |
| EURUSD | core | descriptive_only (match 0.841) | 1719 / 1445 | 048e92586217 | yes |
| BTCUSD | explore | descriptive_only (match 0.883) | 1347 / 1189 | 6f4d09dcc56c | no (n/a) |
| BRENT | explore | passed (match 0.974) | 2008 / 1956 | a7d55af0d97b | no (n/a) |

All failures are match-rate (< 0.90) failures; SMD and censoring differences are within thresholds in the stage partition. No core market is `eligible`.

## Dry-run

`uv run python scripts/observer_gate_c.py --root <backfill> --dry-run`: 190 planned tests, 4 families, B = 24000, all required files resolve for the five
core markets. It reads no Parquet rows, takes no lock (`observer_gate_c_locks` absent before and after) and writes nothing. Preflight output went to a scratch `--out`.

## Consequence

A real `fit` would be REFUSED (not started): all five core markets are `descriptive_only`, so `run_stage` raises the stop rule "no confirmatory test left"
(exit 3) before the registry is written and before the fit lock is taken. No confirmatory Gate C result is possible on the current controls-3 set; the
blocker is the matching rate of the controls-3 builder (0.73 - 0.84 against 0.90), not the Gate C code. Options belong to the owner of the preregistration
(a new preregistration version or a controls builder that reaches the match rate); neither was attempted here. BRENT (explore) passes balance but explore
markets can never confirm.
