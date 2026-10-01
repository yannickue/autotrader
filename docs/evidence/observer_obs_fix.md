# OBS-FIX: Gate C interface, fit-lock order, block length (2026-10-01)

Offline / research only. Counts, manifests and file bytes (hash attestation); no Parquet row values were read beyond label availability counts, no data >= 2026-09-01, no
feature-vs-label distribution, no fit / validate / oos run. Branch `observer/obs-fix` (base `observer/gate-c-run` bd3197a). Prereg-2
(`docs/OBSERVER_GATE_C_PREREGISTRATION.md`) is byte-identical: SHA-256 `4413e35ea3fc7fe38131a3956875ec1402534f10ba55b711d199722300091110` before and after
(pinned by a test that is checkout-independent: CRLF-normalised bytes).

## H1 interface (closed, fail-closed)

* Layout adapter (`resolve_file`, `normalize_balance_gate`) kept and hardened: `controls3` is authoritative, no fallback to the flat controls-2 file when its controls3 file
  is missing (test), controls-2 content with a controls-3 manifest fails the hash check (test), a manifest of the other control set / another market is refused (test).
* Unit: the builder writes fractions, the preregistration speaks pp. The conversion `pp = 100 x fraction` is explicit; a declared unit other than `fraction`, a diff outside
  [-1, 1] (looks like pp) or a pp-valued threshold is refused (test). The builder manifest now declares `censored_share_unit` and `censored_share_denominator`.
* `controls_sha256` is MANDATORY for every used control file (A and B): builder side `backfill_controls3.py` writes `controls_file` + `controls_sha256` into both manifests
  (test). Existing artifacts must NOT be rebuilt: new read-only step `observer_gate_c.py --attest` hashes the bytes of the current files and writes
  `<out>/observer_controls_attestation.json` (never into `--root`; root verified byte/mtime identical before and after). Alternative considered and rejected: a sidecar next to the
  artifacts (would mutate the backfill tree). Limit: trust on first attest, tamper-evident afterwards; it does not prove who built the files. Conflicting declarations,
  missing declaration, changed manifest after attestation, or changed file all fail the preflight; an attestation is never overwritten with different content (exit 6).
* Session tolerance 0.02 / `INSUFFICIENT_N` (< 20 events): present in the builder gate, absent from prereg-2 -> INCONSISTENCY, marked explicitly. Prereg-2 flow: the preflight
  refuses with "PREREG/BUILDER INCONSISTENCY (not bent either way; owner decision needed)" plus the builder-only reason (test). Decision for the future: prereg-3 DECLARES both
  criteria (stricter than prereg-2, never looser), so builder and preflight agree (test). On the real data neither criterion decides anything (all market failures are match rate).
* Censoring denominator: builder = matched pairs, prereg-2 preflight = all stage events vs all stage controls (they differ, and with the opposite sign convention). Prereg-3
  uses matched pairs in both. Cross-check on the real files (counts only): the matched-pair recomputation reproduces the builder's manifest numbers exactly for all seven markets
  (e.g. GER40 +0.200 pp, BRENT +0.716 pp), whereas the prereg-2 recomputation gives -0.724 / -1.096 pp.

Real-data preflight (scratch `--out`, backfill untouched): without attestation every market fails closed ("controls_sha256 REQUIRED", exit 5); after `--attest` the preflight is OK with
the same result as the dry-run evidence: five core markets `descriptive_only` (match rate 0.73-0.84 < 0.90), BTCUSD `descriptive_only`, BRENT `passed`.

## H2 fit-lock order (closed)

`confirmatory_eligibility` runs right after the plan is built and BEFORE the fit lock and the registry: balance eligibility -> confirmatory eligibility (a registered hypothesis of
a CORE market; explore markets never count) -> lock / registry. Nothing eligible: `NoConfirmatoryTestLeft` (stop rule, exit 3), output "INCONCLUSIVE_NOT_ASKED (kein
no-enrichment)", no registry, no report, no lock directory. Previously the stop fired only when the whole plan was empty, so an eligible explore market (BRENT) would have let
the stage proceed, take the lock and register explore hypotheses. Regression test: all core markets descriptive_only + BRENT eligible -> lock dir absent, registry and report absent.

## H3 block length (closed in code behind a version id; prereg-3 is a DRAFT with a FREEZE BLOCKER)

* `observer-stats-3` (`stats.contiguous_day_blocks`, `EnrichmentConfig(stats_version="observer-stats-3", block_unit="tdays", block_len_days>=21)`): contiguous runs of >= 21 distinct
  trading days (remainder merged into the previous block), controls take the block of their event, per-arm block evidence as in stats-2. stats-1 / stats-2 unchanged (their tests
  green); stats-3 refuses day / week blocks and block lengths < 21.
* Choice: contiguous blocks, not a two-way cluster (event day x control day). The defect is the dependence BETWEEN an event and controls up to 10 days away and between controls
  of neighbouring events; resampling event days and control days independently ignores exactly that cross-arm link.
* `docs/OBSERVER_GATE_C_PREREGISTRATION_V3.md` (`observer-gate-c-prereg-3`, namespace `gatec3`, registry `observer_gate_c_prereg3`, own lock): same hypothesis family (tested equal),
  0.90 / 0.10 / 5 pp UNCHANGED (tested), session tolerance + minimum events explicit, one censoring denominator, a fit uses a new registry. The script runs both preregistrations
  (the text selects statistics, namespace, file names). Synthetic end-to-end fit under prereg-3 passes (blocks 3 of >= 21 days, own registry / lock).
* **Freeze blocker (counts only):** the frozen split has ~214-271 business days in TRAIN (max 10-12 blocks of 21) and ~72-91 in VALIDATION (max 3-4 blocks). The carried-over
  minimum of 30 blocks per cell and arm is unreachable, and a bootstrap over 3-4 blocks is degenerate. No threshold was loosened to hide this. Owner decision before freezing:
  lower the block minimum (explicit, justified before any result), pool markets (new preregistration), more history, or accept that the question cannot be asked on this volume.

## Tests (focused; max 2 workers; no whole-repo run)

* `test_gate_c_script.py` (50, of which 14 new) + `test_ol_stats.py` / `_v2` / `_v3` (16 new in v3) + `test_ol_enrichment.py` / `_validity.py`: 106 passed in 181.85 s (wall 3m08s), `-p no:xdist`.
* `test_ol_controls3.py` (builder; manifest hash / unit / denominator assertions added): 18 passed in 329.64 s (wall 5m34s).
* `ruff check` (scripts/observer_gate_c.py, observer_lab, tests/unit/observer_lab) clean; `compileall` clean.

## Backlog (documented, not built)

* Prereg-2 / prereg-3: `NO_EVENTS` / `INSUFFICIENT_N` partitions carry no balance metrics in the builder manifest, so the adapter fails the preflight as a structural error rather
  than marking only that partition descriptive_only; decide whether a market without events in a non-stage partition may still be used for the stage.
* Report columns keep the name `week_*` for the long-block sensitivity under stats-3 (values are the 42-day-block results); rename together with the next report-format change.
* NC-A / NC-B under stats-3 inherit the same block mapping; with 3-4 blocks per partition they are as underpowered as the main test (same blocker).
* The attestation is per `--out`; copy it with the report when archiving a run (it is part of the audit trail next to the fit lock).
* Block minimum / feasibility table should be recomputed from the real trading-day counts (not business-day upper bounds) before the owner decides.
