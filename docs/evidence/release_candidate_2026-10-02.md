# Release candidate 2026-10-02 (final) - integration evidence

Integration only. No deployment, no trading, no push. Branch `release/2026-10-02-final`
(candidate branch `release/2026-10-02-candidate` stays at 027a289).

## Provenance
| Item | SHA |
|---|---|
| Production base (sprint1/integration) | 11c6aec |
| Observer integration (observer/integration) | a23ea8f |
| Observer fix (observer/obs-fix) | 4309f47 (+ 7528555 Gate C interface) |
| Research speed | 74874a1, 3933ea2, d3eba3d (observer/research-speed) |
| Candidate fixes | c885fdf, 58d3401, 027a289 |
| Merge obs-fix | 3252549 |
| Merge research-speed (final head before this doc) | f5ac2da |

Both merges (`--no-ff`) completed WITHOUT textual conflicts (research-speed already contains the obs-fix gate-C
changes). Verified in `scripts/observer_gate_c.py`: FROZEN_PREREG_SHA256 freeze guard, controls_sha256 mandatory + `attest`,
`read_cache_record`/cache/jobs, hardening are all present.

## Invariants
1. Production diff `11c6aec..027a289` vs `11c6aec..HEAD` for src/demo, src/exits, src/nautilus_mt5, src/risk,
   src/execution, scripts/autostart: identical, sha256 of diff output
   `9add5c36ca41808bef5839854362cf63b203840909e0700a6512f4f4faa7288e` for both (857 diff lines);
   `git diff --stat 027a289..HEAD` over these paths is empty.
   No import of research_speed / observer_lab from src/demo, src/market_observer, src/exits, src/risk, src/execution,
   src/nautilus_mt5, scripts/autostart (grep: 0 hits).
2. docs/OBSERVER_GATE_C_PREREGISTRATION.md: sha256 of file bytes (CRLF in checkout) and `frozen_file_sha256()` =
   `4413e35ea3fc7fe38131a3956875ec1402534f10ba55b711d199722300091110` (matches). prereg-3 (`..._V3.md`) is not in
   FROZEN_PREREG_SHA256; `test_prereg3_draft_is_refused_for_every_executing_stage_and_writes_nothing` passes
   (fit/validate/oos -> exit 2, nothing written; --dry-run stays 0).

## Tests (uv run pytest ... -q -p no:xdist, serial)
| Scope | Result | Duration |
|---|---|---|
| tests/unit/observer_lab | 209 passed | 513 s |
| tests/unit/research_speed | 42 passed | 20 s |
| tests/unit/scripts/test_run_tests_tiers.py + test_autostart.py | 84 passed | 23 s |
| tests/unit/demo/test_observer_parity.py + runner/test_runner_observer.py | 31 passed | 286 s |
| tests/unit/demo/runner/test_runner_observer_timing.py (perf, alone, last) | run 1: 1 failed/1 passed; run 2 and 3: 2 passed | ~20 s each |
| ruff check (49 changed files vs 027a289, .py) | all passed | - |
| compileall src scripts tests | exit 0 | 3 s |

Safety suite not repeated: production paths are byte-identical to 027a289 (see invariant 1).

## Known limits
- Perf timing test `test_observer_does_not_delay_the_scan_start_of_any_later_market` failed once (load-sensitive wall-clock
  test, machine busy) and passed in two immediate reruns; thresholds unchanged.
- Running the CLI for the prereg-3 draft on this loaded machine returned exit 4 (research_speed free-memory guard,
  ~500 MB free < 1800 MB needed) BEFORE the freeze guard; the exit-2 behaviour is proven by the unit test, not by the CLI run here.
- No full suite, no integration/safety tiers were run.
