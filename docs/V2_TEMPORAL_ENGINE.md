# V2 Temporal Event Engine + State-Machine Strategies (design, HEAD ce89177)

Status: DESIGN ONLY (Opus, read-only, 2026-09-30). Research-side (`src/alpha/`), never on the live hot path.
V1 (`Genome`, `StrategySpec`, `evaluate_spec`) stays byte-identical. V2 compiles to the existing
`CandidateArrays`, so `simulate_fast`, `light_screen` and `stages` run unchanged. Throughput target and V1
guarantee carry-over still have to be PROVEN by the benchmark/tests named below.

## 0. Verified V1 facts this design builds on (do not break)
- Bar i's signal is known at the M5 close of i. `simulate_fast` fills at `o[i+1]`, skips if `!contig_next[i]` or
  `day[i+1] != day[i]`, one position at a time.
- `CandidateArrays`: `decision_idx` int64 strictly increasing; `direction` +-1; `stop` float64; `target` float64 (finite
  overrides R for EXIT_FIXED_R); `target_r`; `exit_kind` int8 in {FIXED_R, TRAIL}.
- `timeframe._alignment`: an HTF bar b is visible at M5 bar i iff `complete[b]` and `available_at[b] <= ts[i]+5min`.
  `_map_higher` forward-fills. `_run_start(day_id, contig)` = start of the day-contiguous run.
- `_swings`: a pivot at p is exposed at confirmation `p+order`. BUT V1 `bos` is intrabar (`high > known_high`) and repeats
  on every bar: V2 must NOT reuse it as an event (new close-based BOS, one pulse per swing).
- `sweep_*` arrays are float with NaN (unknown). `ThresholdResolver` reads TRAIN bars only; V2 thresholds go through it.

## 1. Event layer
Package `src/alpha/events/`. Separate `EventSet` (dict of 1-D arrays of M5 length N), NOT added to `FeatureSet` (V1 caches
and `FEATURE_SET_VERSION` untouched). Cache key = FeatureStore `cache_key` + `EVENT_SET_VERSION` + hash of event params +
source hash of `alpha/events/*`. One `.npy` per array, loaded with `mmap_mode="r"` lazily.

| prefix | dtype | meaning |
|---|---|---|
| `ev_{tf}_{name}[_{p}]` | uint8 0/1 | PULSE: true only on the bar where the event becomes known (its availability bar) |
| `evl_...` | float64 | level the pulse refers to (swept level, broken swing), NaN if no pulse |
| `evx_...` | float64 | extreme of the event (e.g. sweep wick low), NaN if no pulse |
| `evo_...` | int32 | origin index (pivot bar, pattern start). INFORMATIONAL ONLY: the kernel may never read it |
| `st_{tf}_{name}` | int8 | STATE, step function (trend -1/0/1, inside-zone 0/1) |
| `lv_{tf}_{name}` | float64 | current known level (zone lo/hi, last confirmed swing, trendline value at bar) |
| `zid_{tf}_{name}` | int32 | identity of the current zone/trendline (changes = new object; binds instances) |
| `sf_{name}` | float64/int16 | explicit "so far" quantities: `sf_run_bars`, `sf_{tf}_bar_progress`, `sf_{tf}_partial_h/l`. ONLY place partial HTF data may appear |
| `valid_{tf}` | bool | warm-up/unknown mask; every pulse is 0 where invalid (no NaN in flags) |

HTF rules: (a) STATE/level arrays computed on complete HTF bars, mapped with `_map_higher(alignment)`. (b) A PULSE of HTF bar b is
written to exactly one M5 bar: the first i with `alignment[i] == b`. A data hole moves it to the first bar after the hole, never
earlier. (c) Timeframes: M5, M15, H1, D1 (D1 = previous completed Berlin day; H4/M1 to be added by the multi-market/data lane).
Confirmation-lag rule (hard): every event is stamped at the bar where ALL bars it depends on are closed (pivot = p+order,
pattern = bar of last confirming pivot/close, trendline = confirmation of its 2nd anchor pivot). Window arithmetic uses stamp
indices only; `evo_*` is for reporting.

Catalogue V2.0 (numba, mirror pair L for SHORT): TOUCH(level); BREAK_UP/DN(level) L; RECLAIM_UP/DN L (close through after a
close on the other side within k in {3,6}); RETEST_HOLD_UP/DN L; HOLD_ABOVE/BELOW(k) (state); SWEEP_LOW/HIGH(src) L with
src in {prior20, prior48, PDL, session_low, swing_low}; SWING_LOW/HIGH_CONF L; BOS_UP/DN L (FIRST close beyond the last confirmed swing
since it was confirmed, 1 pulse per swing); CHOCH_UP/DN L (BOS against prevailing structure_state); ZONE_ENTER/EXIT(kind);
TRENDLINE_TOUCH/BREAK L (line through last 2 confirmed pivots, stamped at the 2nd confirmation); PATTERN_COMPLETE(name) (double
bottom/top, inside-bar break; stamped at the confirming close); MOMENTUM_RESUME_UP/DN L; TREND_UP/DN (state). All tol/k/src variants
are a small precomputed discrete set (tol in {0, 0.1, 0.25}); Optuna picks among them and never triggers new event computation.
Memory at N=1e6: ~0.5 GB on disk, batch working set < 150 MB RSS via mmap.

## 2. StateMachineStrategySpec (`src/alpha/temporal/spec.py`)
Bounds: MAX_STATES 6, MAX_STATE_CLAUSES 3, MAX_INVALIDATE 2, MAX_TF 4, MAX_CONTEXT 3, MAX_REG 4, MAX_WITHIN 48, MAX_EXPIRES 96.
Dataclasses (frozen, JSON, hashable): `Clause(kind event|state|feature|bound, name, tf, op IS|NOT|HOLD|BEFORE|SINCE_ENTER, arg, reg,
tol_atr, cmp, q)`; `Capture(reg, source evl|evx|bar_low|bar_high|close|min_low_since_enter|max_high_since_enter|lv, of)`;
`Transition(trigger, within, min_gap=1, guards, invalidate, capture)`; `StopRule(kind register|swing|zone_edge|atr, reg, of, buffer_atr,
atr_mult, max_risk_atr)`; `TargetRule(kind fixed_r|next_structure, r, levels, fallback_r, min_space_r)`;
`StateMachineStrategySpec(strategy_id, version, direction, anchor, anchor_capture, states, context, expires_after, session_window,
stop, target, params, metadata)`; hash scheme as V1 with "schema": "temporal".
validate(): 1..5 transitions, 1..3 anchor clauses, trigger+guards <= 3 per transition, invalidate <= 2, context <= 3; every name
resolves in the event registry; <= 4 distinct timeframes; feature q in [0.05, 0.95]; `min_gap == 1` (no same-bar chaining);
`1 <= within <= MAX_WITHIN`; `expires_after <= MAX_EXPIRES` and >= number of states; typed register dataflow (a bound clause or
StopRule may only read a register captured earlier); HOLD/BEFORE need `1 <= arg <= 48`; pulse+HOLD invalid; LONG-frame names only
(SHORT via mirror table); stop/target ranges (buffer_atr [0,1], r [1,4], min_space_r [0.5,3], max_risk_atr (0,6]); JSON round-trip
with allow_nan=False; floats pre-snapped to grids.
Example (LONG): anchor = H1 TREND_UP + M15 ZONE_ENTER(swing_cluster), capture R0 = zone_lo; T1 = SWEEP_LOW within 8, capture R1 = sweep
wick low, invalidate close below R0; T2 = RECLAIM of R0 within 3, invalidate break below R1; T3 = BOS_UP(M5) within 5, capture R2;
T4 = RETEST_HOLD of R2 within 3; stop = R1 - 0.1 ATR; target = next_structure(H1 swing high, session high, PDH), min_space_r 1.5.

## 3. Evaluation (`src/alpha/temporal/kernel.py`, numba)
All-instances NFA with earliest-advance, strictly causal. A partial match = (anchor_idx, enter_idx, regs[4], runmin, runmax); P_0 =
anchor bars; P_{k+1} = advance(P_k, T_{k+1}). Each part scans forward and advances at its FIRST qualifying bar or dies. An instance
moves at most 1 state per bar; parts are independent. Dedup per stage: equal enter_idx keeps the largest anchor_idx (tie: largest
register tuple) - depends only on data <= enter_idx. Because P_k depends only on (anchor, T_1..T_k), prefixes are shareable EXACTLY.
Order within a bar u: (1) boundary check (run_start change kills the instance), (2) invalidate, (3) trigger, (4) guards (re-checked on
the trigger bar). finalize: context@u, session window, stop from registers/arrays@u (finite, correct side, |c-stop| <= max_risk_atr*atr),
target (fixed R or nearest structure known at u, min_space_r), emit CandidateArrays (EXIT_FIXED_R).
Cost O(|P_k|*W) per stage (W <= 48); batching via a trie over canonical prefix hashes with an LRU of P_k (~48 B/part, 1 GB budget).
Acceptance: `scripts/bench_temporal.py` >= 200 specs/s with 4 workers or the reason is reported. A pure-Python streaming `reference.py`
(bar-by-bar loop; receives only bar u at step u) is the semantic oracle.
Note (honest deviation from O(N*states)): worst case O(N*S*W); the alternative one-live-instance model would break exact prefix sharing.

## 4. Causality tests (`tests/temporal/`, `tests/events/`)
event prefix equality (20 random cut points incl. DST/day ends); future perturbation (randomise OHLC/spread of bars > t; events[:t+1] and
candidates with decision_idx <= t unchanged - on events AND end-to-end); candidate truncation invariance (50 random specs, hypothesis);
kernel vs reference parity (500 random specs, exact equality of all six arrays); prefix-cache equivalence (cache on/off, shuffled batch
order, 1 vs 4 workers -> identical bytes); same-bar ordering (one bar satisfying SWEEP, RECLAIM, BOS advances exactly one state; invalidate+
trigger same bar -> dies; failing guard on trigger bar -> no emit); day/gap boundary incl. last-Sunday DST days (windows counted in
bars); HTF pulse exactly once and none from the open HTF bar; confirmation lag (pulses sit at >= p+order; static test that kernel.py never
names `evo_`); thresholds from TRAIN only (mutating Validation/OOS leaves the compiled program unchanged).

## 5. Genome / search (`src/alpha/discovery/temporal_*.py`)
`TemporalGenome` (LONG frame, frozen, JSON): anchor clause genes, 1..5 step genes (event gene, within, guard preset id, invalidate preset
id), context, stop gene, target gene, time window, lineage. Captures are DERIVED from event registry `produces`/`requires` (not genes), so
dataflow is valid by construction. Typed grammar with roles ANCHOR/SETUP/TRIGGER/CONFIRM/CONTEXT; templates as role paths (ZONE>SWEEP>
RECLAIM>BOS>RETEST, TREND>PULLBACK_HOLD>MOMENTUM_RESUME, RANGE>SWEEP>CHOCH>RETEST). Mutations: insert/delete/replace/swap steps only where the
dataflow allows, `within` on the grid {2,3,5,8,12,24,48}, guard/invalidate presets, stop/target/window/context; crossover one-point on steps
plus register repair; accept only validate()-passing, bounded retries. Optuna per fixed structure over within index, tol variant index,
buffer_atr, max_risk_atr, r/min_space_r, event variant index, q of feature clauses (grid values only; every trial counted in TrialLedger).
Canonical form (fixed point): sort AND-sets, snap to grids, clamp within <= expires_after, drop guard equal to trigger, drop unread captures,
rename registers in first-capture order; hash domain-separated "temporal-v1:". Duplicates: canonical hash; behavior_key after threshold
resolution; behavioural twin = hash of the TRAIN decision_idx stream. QD niches (MAP-Elites): (role-path signature, direction, tf-set bucket,
trade-frequency bucket) with per-niche elite and lineage cap. Complexity = states + clauses + guards + invalidates + distinct tf + fitted params.
`evaluate.py` dispatches on genome type (both return CandidateArrays); the evaluator fingerprint adds EVENT_SET_VERSION, event cache key and
source hashes of alpha/events/* and alpha/temporal/*. Parity test: an anchor-only temporal spec built from V1 rules gives identical
CandidateArrays to evaluate_spec.

## 6. Confluence data contract (`src/alpha/temporal/confluence.py`, dataclasses only)
`SignalRecord` per emitted candidate: signal_id = sha256(spec_hash, decision_idx)[:16], strategy_id, spec_hash, family, lineage_signature,
feature_set_signature, decision_idx, ts_close_ns, direction, entry_zone_lo/hi, stop, target, target_r, anchor_idx, step_idx (enter bar per
state = evidence trail), registers, event_ids (zid of bound zone/trendline). Overlap metrics later: same-bar co-fire Jaccard, shared
anchor/zid rate, feature-set Jaccard, entry-zone IoU. Invariant: a confluence decision at bar u may only read records with decision_idx <= u.

## 7. Build plan (disjoint file ownership)
Phase 0 (serial, freezes interfaces): `src/alpha/events/schema.py` (registry) + `src/alpha/temporal/spec.py` + `tests/temporal/test_spec_validate.py`;
gate: auditor review. Phase 1 (parallel): W1 events (`src/alpha/events/{kernels_price,kernels_structure,kernels_zone,kernels_pattern,
kernels_state,htf,store}.py`, tests/events/*); W2 oracle + contract (`src/alpha/temporal/{reference,confluence}.py`, golden scenarios incl. the
section 2 example); W3 kernel (`src/alpha/temporal/{program,kernel,batch,evaluate}.py`, `scripts/bench_temporal.py`, parity/prefix-cache/contract
tests; uses W2 oracle and synthetic events until W1 lands). Phase 2: end-to-end perturbation + truncation invariance on the real EventStore.
Phase 3 (W4): `src/alpha/discovery/temporal_*` + minimal dispatch hooks in evaluate/deap/optuna drivers. Phase 4: smoke campaign, full suite,
auditor review before interpreting results.

## Top 5 lookahead traps -> catching test
1. HTF data visible before the HTF bar completes -> HTF pulse-once test + perturbation of the open HTF bar's M5 bars.
2. Pivot/pattern/trendline stamped at origin time or windows counted from evo_ -> confirmation-lag test + static no-evo_ check.
3. Intrabar ordering (several transitions, or trigger+invalidate resolved favourably, on one bar) -> same-bar-ordering test + min_gap==1.
4. Repainting levels/zones/registers (zone merging rewriting past lv_, runmin over bars > u, target from a level confirmed after u, dedup
   chosen by outcome) -> prefix equality + end-to-end future perturbation + prefix-cache equivalence.
5. Cross-boundary/global-statistic leakage (instances surviving overnight or contig holes; quantiles from the full sample; V1 intrabar
   `bos` reused as BOS_UP) -> day/gap boundary test, train-only thresholds test, BOS fixture asserting 1 close-based pulse per swing.
Sim-side risk: a structural `target` already crossed by the gap at o[i+1]. Pin simulate_fast's behaviour for a finite target on the wrong
side of the fill with a test BEFORE next_structure targets are used.

## 8. Bound-clause semantics (FROZEN by the orchestrator; kernel and reference oracle MUST implement exactly this)
Bound clauses compare bars against a REGISTER price R captured earlier in the same instance. All windows are counted in M5 bars, are
run-local (never cross a day/contig boundary: the window is clipped at run_start of u), and read only bars <= u. LONG frame shown; SHORT is
the price mirror (swap high/low, flip comparisons). ATR = m5_atr14[u]; tol in {0, 0.1, 0.25} ATR (variant suffix t0/t10/t25).
- TOUCH_REG(R, tol): l[u] <= R + tol*ATR and c[u] > R (touched the level from above and closed above it).
- CLOSE_ABOVE_REG(R, tol): c[u] > R + tol*ATR. CLOSE_BELOW_REG(R, tol): c[u] < R - tol*ATR.
- BREAK_REG(R, tol) (up): c[u] > R + tol*ATR and c[u-1] <= R + tol*ATR (first close through; needs u-1 in the same run, else false).
  Down form (LONG-frame invalidation): c[u] < R - tol*ATR and c[u-1] >= R - tol*ATR.
- RECLAIM_REG(R, k): c[u] > R and there is a bar v in (u-k, u) (k in {3,6}, clipped at run start) with c[v] < R.
- RETEST_HOLD_REG(R, tol, k): exists bar v in (u-k, u) with c[v] > R + tol*ATR[v] (a prior break above), and l[u] <= R + tol*ATR[u] and c[u] > R.
- HOLD_ABOVE(k) as a GUARD on level R: c[t] > R for the last k bars t in (u-k, u] (all k bars must exist inside the run; needs guards_all on the trigger bar).
  HOLD_BELOW mirrored. BEFORE(k) on an event/state: the pulse/state was true at least once in (u-k, u) (strictly before u, run-local).
- A bound clause never reads any bar > u; registers are prices captured at the transition bar (evl/evx/bar_low/bar_high/close/min_low_since_enter
  /max_high_since_enter/lv). min/max_since_enter cover (t_enter, u] inclusive of u and are recomputed only from bars <= u.
- PULLBACK_HOLD is NOT an event: express as state clause (st_{tf}_trend_up) with op HOLD arg k.
