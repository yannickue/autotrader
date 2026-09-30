"""Target-crossed-at-fill guard of ``simulate_fast`` (finite/structural targets only).

Rule: a finite ``target`` must lie strictly beyond the FILL (spread/slippage included) by more
than eps = 1e-9 * max(1, |fill|), and the implied R = beyond / risk must be finite and > 0;
otherwise the candidate is skipped under the appended reason ``target_crossed_at_fill`` without
consuming the position slot or the daily trade cap.  NaN targets (V1) take the untouched path.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from alpha.common.dataset import POINT, load_research_dataset
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.fast import sim as new_sim
from alpha.fast.registry import discover
from alpha.fast.sim import (
    EXIT_FIXED_R,
    REASON_TARGET,
    SKIP_LABELS,
    CandidateArrays,
    MarketArrays,
    simulate_fast,
)
from alpha.fast.store import FeatureStore
from research.runners import ar2_fast

N = 40
COST = COST_SCENARIOS["BASE"]  # spread_mult 1, slippage 0.5, no target penetration
FILL_LONG = 101.5  # o[j]=100 + spread 1 + slip 0.5


def _market(opens: dict[int, float] | None = None) -> MarketArrays:
    o = np.full(N, 100.0)
    for k, v in (opens or {}).items():
        o[k] = v
    minute = 600 + 5 * np.arange(N)
    contig = np.ones(N, dtype=bool)
    contig[-1] = False
    return MarketArrays(
        o, o + 0.5, o - 0.5, o.copy(), np.full(N, 1.0), minute, np.zeros(N, dtype=np.int64), contig
    )


def _cands(rows: list[tuple[int, int, float, float]]) -> CandidateArrays:
    """rows: (decision_idx, direction, stop, target)."""
    return CandidateArrays(
        decision_idx=np.array([r[0] for r in rows], dtype=np.int64),
        direction=np.array([r[1] for r in rows], dtype=np.int8),
        stop=np.array([r[2] for r in rows]),
        target=np.array([r[3] for r in rows]),
        target_r=np.full(len(rows), 2.0),
        exit_kind=np.full(len(rows), EXIT_FIXED_R, dtype=np.int8),
    )


def test_skip_label_appended_existing_unchanged() -> None:
    assert SKIP_LABELS[:9] == (
        "no_next_bar", "gap_before_entry", "outside_window", "day_cap", "spread_filter",
        "stop_invalid", "risk_out_of_range", "size_below_min", "entry_gap_stop",
    )
    assert SKIP_LABELS[9] == "target_crossed_at_fill" and len(SKIP_LABELS) == 10
    t = simulate_fast(_market(), _cands([(4, 1, 95.0, 99.0)]), COST)
    assert len(t.skip_counts) == 10 and t.skips["target_crossed_at_fill"] == 1


def test_long_and_short_target_crossed_at_fill_skipped_and_counted() -> None:
    long_t = simulate_fast(_market(), _cands([(4, 1, 95.0, 99.0)]), COST)
    assert len(long_t) == 0 and long_t.skips["target_crossed_at_fill"] == 1
    short_t = simulate_fast(_market(), _cands([(4, -1, 105.0, 101.0)]), COST)  # short fill 99.5
    assert len(short_t) == 0 and short_t.skips["target_crossed_at_fill"] == 1
    below_stop = simulate_fast(_market(), _cands([(4, 1, 95.0, 90.0)]), COST)
    assert len(below_stop) == 0 and below_stop.skips["target_crossed_at_fill"] == 1


def test_target_valid_at_decision_but_crossed_by_gap_is_skipped() -> None:
    t = simulate_fast(_market({5: 102.0}), _cands([(4, 1, 95.0, 100.5)]), COST)
    assert len(t) == 0 and t.skips["target_crossed_at_fill"] == 1


def test_target_exactly_at_fill_is_skipped() -> None:
    long_t = simulate_fast(_market(), _cands([(4, 1, 95.0, FILL_LONG)]), COST)
    assert long_t.skips["target_crossed_at_fill"] == 1 and len(long_t) == 0
    short_t = simulate_fast(_market(), _cands([(4, -1, 105.0, 99.5)]), COST)  # short fill = 99.5
    assert len(short_t) == 0 and short_t.skips["target_crossed_at_fill"] == 1


def test_target_beyond_fill_trades_and_exits_at_target() -> None:
    m = _market()
    m.h[8] = 110.0  # target reachable
    t = simulate_fast(m, _cands([(4, 1, 95.0, 105.0)]), COST)
    assert len(t) == 1 and t.skips["target_crossed_at_fill"] == 0
    assert t.exit_reason[0] == REASON_TARGET and t.exit_price[0] == 105.0
    assert t.exit_idx[0] > t.entry_idx[0] and t.r_multiple[0] > 0
    gross_r = (105.0 - FILL_LONG) / (FILL_LONG - 95.0)
    assert abs(t.r_multiple[0] - gross_r) < 0.1  # ~ (target-fill)/risk, net of costs


def test_slot_not_consumed_by_skipped_candidate() -> None:
    m = _market()
    m.h[8] = 110.0
    # first candidate (decision 4) is crossed; second (decision 5, the very next bar) must trade
    t = simulate_fast(m, _cands([(4, 1, 95.0, 99.0), (5, 1, 95.0, 105.0)]), COST)
    assert len(t) == 1 and t.entry_idx[0] == 6 and t.skips["target_crossed_at_fill"] == 1


def test_max_trades_per_day_not_consumed_by_skipped_candidate() -> None:
    m = _market()
    m.h[8] = 110.0
    rules = SimRules(max_trades_per_day=1)
    t = simulate_fast(m, _cands([(4, 1, 95.0, 99.0), (5, 1, 95.0, 105.0)]), COST, rules=rules)
    assert len(t) == 1 and t.skips["day_cap"] == 0 and t.skips["target_crossed_at_fill"] == 1


def test_non_finite_target_means_fixed_r_path_and_is_not_guarded() -> None:
    """Only a FINITE target is a structural target; inf/NaN keep the fixed-R path (V1 semantics)."""
    a = simulate_fast(_market(), _cands([(4, 1, 95.0, np.inf)]), COST)
    b = simulate_fast(_market(), _cands([(4, 1, 95.0, np.nan)]), COST)
    assert len(a) == len(b) == 1 and a.skips["target_crossed_at_fill"] == 0
    np.testing.assert_array_equal(a.exit_price, b.exit_price)


# ---- V1 bit-identity against the untouched HEAD kernel ---------------------------------------


def _load_old_sim(tmp_path: Path):
    src = subprocess.run(
        ["git", "show", "76dcff3:src/alpha/fast/sim.py"], capture_output=True, check=True,
        cwd=Path(__file__).resolve().parents[1],
    ).stdout.decode("utf-8")
    path = tmp_path / "old_fast_sim.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("old_fast_sim", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["old_fast_sim"] = mod
    spec.loader.exec_module(mod)
    return mod


def _to_old(old, market: MarketArrays, cand: CandidateArrays):
    m = old.MarketArrays(**{f.name: getattr(market, f.name) for f in dataclasses.fields(market)})
    c = old.CandidateArrays(**{f.name: getattr(cand, f.name) for f in dataclasses.fields(cand)})
    return m, c


def _assert_identical(new_t, old_t) -> None:
    for f in dataclasses.fields(new_t):
        a, b = getattr(new_t, f.name), getattr(old_t, f.name)
        if f.name == "skip_counts":
            assert len(a) == 10 and len(b) == 9
            np.testing.assert_array_equal(a[:9], b)
            assert a[9] == 0
        else:
            np.testing.assert_array_equal(a, b, err_msg=f.name)


def test_v1_golden_variants_bit_identical_to_head_kernel(tmp_path: Path) -> None:
    old = _load_old_sim(tmp_path)
    cfg = json.loads(ar2_fast.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    ds = load_research_dataset(ar2_fast.REPO_ROOT / cfg["dataset_root"])
    frame = ar2_fast.dev_frame(ds.frame, ar2_fast._plan(cfg))
    features = FeatureStore.load_or_build(frame, {"point_size": POINT}, tmp_path / "features")
    market = ar2_fast._market(features)
    sizing = SizingSpec(**cfg["sizing"])
    rules = SimRules(**cfg["rules"])
    variants = [
        (family, params) for _, family in sorted(discover().items()) for params in family.variants
    ]
    checked = 0
    for family, params in variants:
        cand = family.generate(features, params)
        assert np.isnan(cand.target).all()  # V1 = fixed R
        new_t = simulate_fast(market, cand, COST, sizing, rules)
        if not len(new_t):
            continue
        om, oc = _to_old(old, market, cand)
        _assert_identical(new_t, old.simulate_fast(om, oc, COST, sizing, rules))
        checked += 1
        if checked == 3:
            break
    assert checked == 3
    assert new_sim.SKIP_LABELS[:9] == old.SKIP_LABELS
