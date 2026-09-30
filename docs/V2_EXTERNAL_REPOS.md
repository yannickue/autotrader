# V2 external repository classification (2026-09-30)

Sources: Haiku scout (web) for descriptions; licence / last-push / archived facts below were
VERIFIED against the GitHub API by the orchestrator. The scout got three items wrong or
inconsistent (pymarket-structure repo, AlphaGen 'USE DIRECTLY' despite no licence, a
few unverified Windows/py3.12 claims); corrected here. Rule: never import external signal
code blindly; every algorithm we adopt is re-implemented causally and passes the truncation
tests (prefix equality, DST/day boundaries).

| project | repo | licence (verified) | last push | verdict | notes |
|---|---|---|---|---|---|
| pymarket-structure | fortunato/pymarket-structure | MIT | 2026-06-04 | ADAPT ALGORITHM (review first) | swings, trends, support/resistance zones, structure; audit pivot-confirmation lag / repainting before any use |
| pytrendline | ednunezg/pytrendline | MIT | 2024-11-13 | ADAPT ALGORITHM | line-fit idea useful; retrospective, brute force: use only with confirmed pivots and bounded candidate sets, never in the hot path |
| PyIndicators | coding-kitties/PyIndicators | MIT | 2026-03-04 | REFERENCE ONLY | duplicates TA-Lib role |
| TradingPatternScanner | white07S/TradingPatternScanner | NOASSERTION (README reportedly non-commercial share-alike) | 2023-08-02 | REFERENCE ONLY | licence unusable; retrospective detection |
| chart_patterns | zeta-zetra/chart_patterns | NONE | 2024-07-08 | REFERENCE ONLY | no licence = all rights reserved; do not copy code |
| AlphaGen | RL-MLDM/alphagen | NONE | 2026-06-04 | REFERENCE ONLY (idea) | no licence: do NOT vendor or redistribute; re-implement the formula-operator + pool/RL/GP idea on our Feature Store (or ask the authors / owner decision to run it locally as a black box) |
| AutoAlpha | szy1900/autoAlpha | NONE | 2023-07-03 | REFERENCE ONLY | paper ideas (quality-diversity via PCA of alpha space) |
| RD-Agent | microsoft/RD-Agent | MIT | 2026-09-23 | USE DIRECTLY (bounded experiment, offline) | LLM propose/implement/evaluate loop; heavy deps, isolate in its own venv/worktree; outputs are DATA (factors) fed through our causal validation |
| Qlib | microsoft/qlib | MIT | 2026-09-22 | USE (formula operators / data layer for RD-Agent), isolated | not the execution engine; DL leakage audit required |
| tsfresh | blue-yonder/tsfresh | MIT | 2026-07-06 | USE DIRECTLY (feature-relevance filtering) | only on causal rolling windows |
| FreqAI | freqtrade/freqtrade | GPL-3.0 | active | REFERENCE ONLY | GPL incompatible; ideas: adaptive retraining, outlier handling |
| Footprint Studio | (not verified) | - | - | REFERENCE ONLY | true order flow needs exchange data, separate future lane |

Most valuable ideas: pivot-confirmation stamping (availability = confirmation time), bounded
trendline candidate fitting from confirmed pivots, AlphaGen-style formula pool with diversity
pressure, RD-Agent hypothesis->implementation->evaluation->critique loop run offline, tsfresh
relevance testing as a feature filter for causal windows.
