# ruff: noqa: E501
"""Lane X: ENTRY quality vs EXIT / profit-capture quality, measured SEPARATELY (diagnostics only).

Binding principle: a negative final R does NOT mean a bad entry.  A trade that reached +0.60R and then lost 1R
is a POTENTIAL_USEFUL_ENTRY with POOR_PROFIT_CAPTURE (EXIT_GIVEBACK); a trade with MFE ~ 0R that goes straight to
-1R is an ENTRY_FAILURE.  The two axes are classified and reported independently; they are never collapsed into
one final-R number.

Everything here is pure (no IO, no MT5, no persistence) and CAUSAL: a measurement uses only the bars AFTER the
entry (the entry's own future is the outcome) and, for structure, only levels confirmed before the entry.

Versioned methodology (written into every output)
  ANALYSIS_VERSION        the whole measurement (paths, levels, semantics below)
  LABEL_SPEC_VERSION      the classification thresholds (``LabelThresholds``) - PREDECLARED, never tuned on results

Bar semantics (mirror ``demo.labeling.simulate_hypothetical`` / ``alpha.fast.sim``)
  * prices are EXIT-SIDE: long exits at the bid (bar prices), short exits at the ask (= bid + bar spread);
  * inside one bar the STOP is checked before any favourable excursion (pessimistic stop-first); a bar that opens
    through the stop exits at its open (gap); MAE is updated on the stop bar, MFE is NOT;
  * a bar is dated by its OPEN (the existing ``path_analytics`` convention): ``time_to_* = 0`` means "reached in
    the entry bar"; resolution = one bar (300 s for M5);
  * the entry path ends at the first of: initial stop, (optional) target, flat deadline, end of data.  It does NOT
    end at a fixed-R target when ``target`` is None: that is the POTENTIAL path the entry offered.

MFE_CAPTURE_RATIO (defined carefully)
  capture = final_r / mfe_r, defined only when mfe_r >= ``min_mfe_for_capture`` (0.10R; below that the ratio is
  numerically meaningless and reported as None: "no favourable excursion").  ``final_r`` is the realised
  (gross, spread-adjusted) R of the exit being judged.  It is SIGNED: a winner that exits at its peak is 1.0, a
  winner that gave half back 0.5, a trade that went +0.6R and lost 1R is -1.667 (losers are negative by design).
  ``capture_ratio_floored`` = clip(max(final_r, 0) / mfe_r, 0, 1) is the aggregation-friendly version.
  MFE_GIVEBACK = max(0, mfe_r - final_r) in R (existing ``mfe_giveback_r`` semantics).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

ANALYSIS_VERSION = "eeq-1.0"
LABEL_SPEC_VERSION = "eeq-labels-1"

MFE_LEVELS_R: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
FIRST_TOUCH_R = 0.5  # MFE-before-MAE: which of +0.5R favourable / -0.5R adverse was touched first
BAR_SECONDS = 300
SMALL_N = 30  # below this many raw entries a cell is flagged "n too small"
MIN_CLUSTERS = 20  # below this many independent event clusters no verdict is given

ENTRY_USEFUL = "POTENTIAL_USEFUL_ENTRY"
ENTRY_FAILURE = "ENTRY_FAILURE"
ENTRY_AMBIGUOUS = "AMBIGUOUS_ENTRY"
CAPTURE_GOOD = "GOOD_PROFIT_CAPTURE"
CAPTURE_POOR = "POOR_PROFIT_CAPTURE"  # == EXIT_GIVEBACK
CAPTURE_NA = "CAPTURE_NOT_APPLICABLE"
LABEL_GOOD = "GOOD_ENTRY_GOOD_CAPTURE"
LABEL_GIVEBACK = "POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK"
LABEL_FAILURE = "ENTRY_FAILURE"
LABEL_AMBIGUOUS = "AMBIGUOUS"
LABELS = (LABEL_GOOD, LABEL_GIVEBACK, LABEL_FAILURE, LABEL_AMBIGUOUS)

FIRST_FAV = "FAV_FIRST"
FIRST_ADV = "ADV_FIRST"
FIRST_NEITHER = "NEITHER"

VERDICT_BAD_ENTRIES = "BAD ENTRIES"
VERDICT_USEFUL_BAD_CAPTURE = "USEFUL ENTRIES + BAD CAPTURE"
VERDICT_BOTH = "BOTH"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE-n"
VERDICT_NO_DEFICIT = "NO CLEAR DEFICIT"


def random_walk_reach_probability(level_r: float) -> float:
    """Reference ONLY: for a driftless continuous price path the probability to touch +``level_r`` R before the -1R stop
    is 1 / (1 + level_r) (gambler's ruin).  A cell's P(MFE >= x) must be read against it: with zero edge about two
    thirds of all entries reach +0.5R before the stop, and the share of ENTRY_FAILURE / useful labels is therefore
    NOT evidence of entry quality by itself (spreads and discrete bars push the null slightly lower)."""
    return 1.0 / (1.0 + level_r)


RANDOM_WALK_REFERENCE: dict[str, float] = {f"{x:g}": random_walk_reach_probability(x) for x in MFE_LEVELS_R}


@dataclass(frozen=True, slots=True)
class LabelThresholds:
    """PREDECLARED classification thresholds (version ``LABEL_SPEC_VERSION``); not tuned on any result."""

    useful_mfe_r: float = 0.5  # MFE >= this: the entry offered a meaningful favourable excursion
    failure_mfe_r: float = 0.25  # MFE < this: no meaningful favourable excursion ...
    failure_mae_r: float = 0.75  # ... and MAE >= this: price went (nearly) straight to the stop
    fast_mae_s: float = 1800.0  # sub-flag: adverse extreme reached within 30 min
    good_capture: float = 0.5  # capture_ratio >= this: half or more of the excursion was kept
    min_mfe_for_capture: float = 0.10
    # verdict shares (per cell, over its entries)
    bad_entry_failure_share: float = 0.40
    useful_share_min: float = 0.40
    both_useful_share_min: float = 0.20
    poor_capture_share: float = 0.50

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in self.__slots__}


DEFAULT_THRESHOLDS = LabelThresholds()


class Step(NamedTuple):
    """One bar in EXIT-SIDE prices.  ``o`` / ``c`` may be None (live tick/bar path without them)."""

    ts: datetime
    o: float | None
    hi: float
    lo: float
    c: float | None


@dataclass(slots=True)
class PathMetrics:
    mfe_r: float = 0.0
    mae_r: float = 0.0
    time_to_mfe_s: float | None = None
    time_to_mae_s: float | None = None
    reach_s: dict[float, float | None] = field(default_factory=dict)  # level R -> seconds (None = never)
    first_touch: str = FIRST_NEITHER
    bars: int = 0
    exit_kind: str = "DATA_END"  # STOP | STOP_GAP | TARGET | FLAT | DATA_END | PATH_END
    bars_above_entry: int | None = None
    bars_below_entry: int | None = None
    tp1_reached: bool | None = None
    tp2_reached: bool | None = None
    time_to_tp1_s: float | None = None
    time_to_tp2_s: float | None = None
    max_structure_reached_r: float | None = None
    holding_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mfe_r": self.mfe_r, "mae_r": self.mae_r, "time_to_mfe_s": self.time_to_mfe_s,
            "time_to_mae_s": self.time_to_mae_s, "first_touch_0.5R": self.first_touch,
            "mfe_before_mae": {FIRST_FAV: True, FIRST_ADV: False}.get(self.first_touch), "bars": self.bars,
            "path_exit_kind": self.exit_kind, "bars_above_entry": self.bars_above_entry,
            "bars_below_entry": self.bars_below_entry, "tp1_reached": self.tp1_reached, "tp2_reached": self.tp2_reached,
            "time_to_tp1_s": self.time_to_tp1_s, "time_to_tp2_s": self.time_to_tp2_s,
            "max_structure_reached_r": self.max_structure_reached_r, "holding_s": self.holding_s,
        }
        for x in MFE_LEVELS_R:
            d[f"time_to_{x:g}R_s"] = self.reach_s.get(x)
            d[f"reached_{x:g}R"] = self.reach_s.get(x) is not None
        return d


def walk_entry_path(
    *,
    direction: int,
    entry: float,
    stop: float,
    steps: Iterable[Step],
    entry_ts: datetime,
    apply_stop: bool = True,
    target: float | None = None,
    flat_ts: datetime | None = None,
    tp1: float | None = None,
    tp2: float | None = None,
    structure_levels: Sequence[float] = (),
) -> PathMetrics:
    """Entry-quality measurement over the bars AFTER the entry (EXIT-SIDE prices, see the module doc).

    ``steps`` must be time-ordered and start at or after ``entry_ts``; a step at/after ``flat_ts`` is NOT used
    (the forced flat closes the position at its open).  ``apply_stop=False`` is for an already-closed real path
    (the stop / exit already happened; nothing is truncated).  ``tp1`` / ``tp2`` / ``structure_levels`` are price
    levels that were confirmed BEFORE the entry (causal); they are only measured, never used as exits here."""
    risk = abs(entry - stop)
    if not risk > 0:
        raise ValueError("initial risk distance must be > 0")
    if direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        raise ValueError("stop must be on the adverse side of entry")
    long = direction > 0
    m = PathMetrics(reach_s={x: None for x in MFE_LEVELS_R})
    has_close = True
    above = below = 0
    tp1_hit = tp2_hit = False
    lvl_reached: list[float] = []
    last_ts = entry_ts
    for s in steps:
        if flat_ts is not None and s.ts >= flat_ts:
            m.exit_kind = "FLAT"
            break
        t = max((s.ts - entry_ts).total_seconds(), 0.0)
        last_ts = s.ts
        m.bars += 1
        adv_px = (entry - s.lo) if long else (s.hi - entry)
        fav_px = (s.hi - entry) if long else (entry - s.lo)
        adv_r, fav_r = adv_px / risk, fav_px / risk
        if adv_r > m.mae_r:
            m.mae_r, m.time_to_mae_s = adv_r, t
        if apply_stop:
            gap = s.o is not None and ((s.o <= stop) if long else (s.o >= stop))
            hit = (s.lo <= stop) if long else (s.hi >= stop)
            if gap or hit:
                m.exit_kind = "STOP_GAP" if gap else "STOP"
                if m.first_touch == FIRST_NEITHER and adv_r >= FIRST_TOUCH_R:
                    m.first_touch = FIRST_ADV
                break
        if target is not None and ((s.hi >= target) if long else (s.lo <= target)):
            t_r = abs(target - entry) / risk
            if t_r > m.mfe_r:
                m.mfe_r, m.time_to_mfe_s = t_r, t
            for x in MFE_LEVELS_R:
                if m.reach_s[x] is None and t_r >= x:
                    m.reach_s[x] = t
            m.exit_kind = "TARGET"
            break
        if m.first_touch == FIRST_NEITHER:
            if adv_r >= FIRST_TOUCH_R:  # same-bar tie: adverse first (conservative)
                m.first_touch = FIRST_ADV
            elif fav_r >= FIRST_TOUCH_R:
                m.first_touch = FIRST_FAV
        if fav_r > m.mfe_r:
            m.mfe_r, m.time_to_mfe_s = fav_r, t
        for x in MFE_LEVELS_R:
            if m.reach_s[x] is None and fav_r >= x:
                m.reach_s[x] = t
        if s.c is None:
            has_close = False
        else:
            c_fav = (s.c - entry) if long else (entry - s.c)
            if c_fav > 0:
                above += 1
            elif c_fav < 0:
                below += 1
        if tp1 is not None and not tp1_hit and ((s.hi >= tp1) if long else (s.lo <= tp1)):
            tp1_hit, m.time_to_tp1_s = True, t
        if tp2 is not None and not tp2_hit and ((s.hi >= tp2) if long else (s.lo <= tp2)):
            tp2_hit, m.time_to_tp2_s = True, t
        for lv in structure_levels:
            if ((lv > entry) if long else (lv < entry)) and ((s.hi >= lv) if long else (s.lo <= lv)):
                lvl_reached.append(abs(lv - entry) / risk)
    else:
        if m.exit_kind == "DATA_END" and not apply_stop:
            m.exit_kind = "PATH_END"
    m.holding_s = max((last_ts - entry_ts).total_seconds(), 0.0)
    if has_close:
        m.bars_above_entry, m.bars_below_entry = above, below
    if tp1 is not None:
        m.tp1_reached = tp1_hit
    if tp2 is not None:
        m.tp2_reached = tp2_hit
    if tp1 is not None or tp2 is not None or structure_levels:
        cands = list(lvl_reached)
        if tp1_hit and tp1 is not None:
            cands.append(abs(tp1 - entry) / risk)
        if tp2_hit and tp2 is not None:
            cands.append(abs(tp2 - entry) / risk)
        m.max_structure_reached_r = max(cands) if cands else 0.0
    return m


# ---- classification -------------------------------------------------------------------------------
def capture_ratio(mfe_r: float, final_r: float, th: LabelThresholds = DEFAULT_THRESHOLDS) -> tuple[float | None, float | None]:
    """(signed ratio, floored ratio in [0,1]); both None when ``mfe_r`` < ``min_mfe_for_capture``."""
    if not (mfe_r >= th.min_mfe_for_capture):
        return None, None
    ratio = final_r / mfe_r
    floored = min(1.0, max(0.0, final_r) / mfe_r)
    return ratio, floored


def classify(
    *, mfe_r: float, mae_r: float, final_r: float, time_to_mae_s: float | None = None,
    th: LabelThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """Two independent axes + their combination (see the module doc and ``LabelThresholds``).

    entry axis   POTENTIAL_USEFUL_ENTRY   mfe >= useful_mfe_r
                 ENTRY_FAILURE            mfe < failure_mfe_r AND mae >= failure_mae_r
                 AMBIGUOUS_ENTRY          everything else
    capture axis (only judged for a useful entry)
                 GOOD_PROFIT_CAPTURE      capture_ratio >= good_capture
                 POOR_PROFIT_CAPTURE      otherwise (EXIT_GIVEBACK)
    combined     GOOD_ENTRY_GOOD_CAPTURE | POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK | ENTRY_FAILURE | AMBIGUOUS"""
    ratio, floored = capture_ratio(mfe_r, final_r, th)
    giveback = max(0.0, mfe_r - final_r)
    if mfe_r >= th.useful_mfe_r:
        entry = ENTRY_USEFUL
        cap = CAPTURE_GOOD if (ratio is not None and ratio >= th.good_capture) else CAPTURE_POOR
        label = LABEL_GOOD if cap == CAPTURE_GOOD else LABEL_GIVEBACK
    elif mfe_r < th.failure_mfe_r and mae_r >= th.failure_mae_r:
        entry, cap, label = ENTRY_FAILURE, CAPTURE_NA, LABEL_FAILURE
    else:
        entry, cap, label = ENTRY_AMBIGUOUS, CAPTURE_NA, LABEL_AMBIGUOUS
    return {
        "entry_label": entry, "capture_label": cap, "entry_exit_label": label,
        "fast_failure": bool(entry == ENTRY_FAILURE and time_to_mae_s is not None and time_to_mae_s <= th.fast_mae_s),
        "capture_ratio": ratio, "capture_ratio_floored": floored, "mfe_giveback_r": giveback,
        "final_r_judged": final_r,
    }


def entry_exit_fields(
    *, direction: int, entry: float, stop: float, steps: Sequence[Step], entry_ts: datetime, final_r: float,
    apply_stop: bool = False, target: float | None = None, flat_ts: datetime | None = None,
    tp1: float | None = None, tp2: float | None = None, structure_levels: Sequence[float] = (),
    th: LabelThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """Flat, JSON-friendly entry-vs-exit fields (forward hook + offline harness): path metrics + classification."""
    pm = walk_entry_path(
        direction=direction, entry=entry, stop=stop, steps=steps, entry_ts=entry_ts, apply_stop=apply_stop,
        target=target, flat_ts=flat_ts, tp1=tp1, tp2=tp2, structure_levels=structure_levels,
    )
    out = pm.as_dict()
    out.update(classify(mfe_r=pm.mfe_r, mae_r=pm.mae_r, final_r=final_r, time_to_mae_s=pm.time_to_mae_s, th=th))
    out["eeq_version"] = ANALYSIS_VERSION
    out["eeq_label_spec"] = LABEL_SPEC_VERSION
    return out


# ---- clustering (statistical dependence) -----------------------------------------------------------
CLUSTER_WINDOW_S = 3600.0  # nearby same-market same-direction signals within 1 h share an event cluster
CROSS_MARKET_WINDOW_S = 900.0  # same-direction signals of correlated markets within 15 min share a market cluster


def assign_event_clusters(rows: Sequence[Mapping[str, Any]], window_s: float = CLUSTER_WINDOW_S) -> list[str]:
    """Event-cluster id per row.  A row carrying ``structure_event_id`` is keyed by it (four STRUCT variants of one
    break are ONE observation); any other row is chain-linked with earlier same-market same-direction rows whose
    decision time lies within ``window_s`` (greedy, chronological).  Rows need ``market``, ``direction`` and
    ``signal_ts`` (datetime)."""
    order = sorted(range(len(rows)), key=lambda i: rows[i]["signal_ts"])
    ids: dict[int, str] = {}
    last: dict[tuple[str, int], tuple[datetime, str]] = {}
    for i in order:
        r = rows[i]
        ev = r.get("structure_event_id")
        if ev:
            ids[i] = f"EV:{ev}"
            continue
        key = (str(r["market"]), int(r["direction"]))
        prev = last.get(key)
        if prev is not None and (r["signal_ts"] - prev[0]).total_seconds() <= window_s:
            cid = prev[1]
        else:
            cid = f"C:{key[0]}:{key[1]}:{r['signal_ts'].isoformat()}"
        ids[i] = cid
        last[key] = (r["signal_ts"], cid)
    return [ids[i] for i in range(len(rows))]


def assign_market_clusters(
    rows: Sequence[Mapping[str, Any]], cluster_of: Any, window_s: float = CROSS_MARKET_WINDOW_S,
) -> list[str]:
    """Cross-market cluster id: same market cluster (GER40/NAS100/SPX500 = INDEX ...), same direction, decision
    times chain-linked within ``window_s``.  Uses the existing risk-policy cluster config (``cluster_of``)."""
    order = sorted(range(len(rows)), key=lambda i: rows[i]["signal_ts"])
    ids: dict[int, str] = {}
    last: dict[tuple[str, int], tuple[datetime, str]] = {}
    for i in order:
        r = rows[i]
        key = (str(cluster_of(str(r["market"])) or r["market"]), int(r["direction"]))
        prev = last.get(key)
        if prev is not None and (r["signal_ts"] - prev[0]).total_seconds() <= window_s:
            cid = prev[1]
        else:
            cid = f"M:{key[0]}:{key[1]}:{r['signal_ts'].isoformat()}"
        ids[i] = cid
        last[key] = (r["signal_ts"], cid)
    return [ids[i] for i in range(len(rows))]


# ---- summarising ----------------------------------------------------------------------------------
def _mean(xs: Sequence[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


def cluster_equal_mean(values: Sequence[float | None], clusters: Sequence[str]) -> float | None:
    """Mean of the per-cluster means (each independent event counts once)."""
    by: dict[str, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        if v is not None:
            by.setdefault(c, []).append(v)
    return _mean([sum(v) / len(v) for v in by.values()]) if by else None


def verdict_for(
    *, n: int, n_clusters: int, failure_share: float, useful_share: float, poor_capture_share_of_useful: float | None,
    th: LabelThresholds = DEFAULT_THRESHOLDS,
) -> str:
    """One-line category from PREDECLARED shares (see ``LabelThresholds``); small n / few clusters -> INCONCLUSIVE."""
    if n < SMALL_N or n_clusters < MIN_CLUSTERS:
        return VERDICT_INCONCLUSIVE
    poor = poor_capture_share_of_useful is not None and poor_capture_share_of_useful >= th.poor_capture_share
    if failure_share >= th.bad_entry_failure_share and useful_share >= th.both_useful_share_min and poor:
        return VERDICT_BOTH
    if failure_share >= th.bad_entry_failure_share:
        return VERDICT_BAD_ENTRIES
    if useful_share >= th.useful_share_min and poor:
        return VERDICT_USEFUL_BAD_CAPTURE
    return VERDICT_NO_DEFICIT


def summarise_entries(
    rows: Sequence[Mapping[str, Any]], *, event_clusters: Sequence[str], th: LabelThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """Entry-quality / capture summary of one cell.  Rows carry the flat ``entry_exit_fields`` keys plus
    ``baseline_r`` (and optional ``policy_r`` = {policy_id: R or None})."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "n_event_clusters": 0, "flag": "n=0", "verdict": VERDICT_INCONCLUSIVE}
    mfe = [float(r["mfe_r"]) for r in rows]
    mae = [float(r["mae_r"]) for r in rows]
    labels = [r["entry_exit_label"] for r in rows]
    n_clusters = len(set(event_clusters))
    useful = [r for r in rows if r["entry_label"] == ENTRY_USEFUL]
    failures = [r for r in rows if r["entry_label"] == ENTRY_FAILURE]
    poor_useful = [r for r in useful if r["capture_label"] == CAPTURE_POOR]
    cap = [float(r["capture_ratio_floored"]) for r in rows if r.get("capture_ratio_floored") is not None]
    cap_signed = [float(r["capture_ratio"]) for r in rows if r.get("capture_ratio") is not None]
    first = [r.get("first_touch_0.5R") for r in rows]
    base = [float(r["baseline_r"]) for r in rows if r.get("baseline_r") is not None]
    base_cl = cluster_equal_mean([r.get("baseline_r") for r in rows], event_clusters)
    policy_ids = sorted({p for r in rows for p in (r.get("policy_r") or {})})
    policy_stats: dict[str, Any] = {}
    for p in policy_ids:
        vals = [(r["policy_r"].get(p), c) for r, c in zip(rows, event_clusters, strict=True) if r.get("policy_r")]
        ok = [(v, c) for v, c in vals if v is not None]
        caps = [r["policy_capture"][p] for r in rows if (r.get("policy_capture") or {}).get(p) is not None]
        policy_stats[p] = {
            "n_applicable": len(ok), "mean_r": _mean([v for v, _ in ok]), "median_r": _median([v for v, _ in ok]),
            "mean_r_cluster_equal": cluster_equal_mean([v for v, _ in ok], [c for _, c in ok]),
            "capture_ratio_floored_mean": _mean(caps), "n_capture_defined": len(caps),
        }
    failure_share = len(failures) / n
    useful_share = len(useful) / n
    poor_share = (len(poor_useful) / len(useful)) if useful else None
    out = {
        "n": n, "n_event_clusters": n_clusters,
        "flag": f"n too small (<{SMALL_N}): no conclusion" if n < SMALL_N else (f"few independent clusters (<{MIN_CLUSTERS})" if n_clusters < MIN_CLUSTERS else "ok"),
        "mfe_mean": _mean(mfe), "mfe_median": _median(mfe), "mae_mean": _mean(mae), "mae_median": _median(mae),
        "p_mfe_ge": {f"{x:g}": sum(1 for v in mfe if v >= x) / n for x in MFE_LEVELS_R},
        "median_time_to_mfe_s": _median([r["time_to_mfe_s"] for r in rows if r.get("time_to_mfe_s") is not None]),
        "median_time_to_mae_s": _median([r["time_to_mae_s"] for r in rows if r.get("time_to_mae_s") is not None]),
        "median_time_to_R_s": {f"{x:g}": _median([r[f"time_to_{x:g}R_s"] for r in rows if r.get(f"time_to_{x:g}R_s") is not None]) for x in MFE_LEVELS_R},
        "share_mfe_first": sum(1 for f in first if f == FIRST_FAV) / n,
        "share_mae_first": sum(1 for f in first if f == FIRST_ADV) / n,
        "share_neither_first": sum(1 for f in first if f == FIRST_NEITHER) / n,
        "mfe_giveback_mean": _mean([float(r["mfe_giveback_r"]) for r in rows]),
        "capture_ratio_floored_mean": _mean(cap), "capture_ratio_signed_median": _median(cap_signed),
        "n_capture_defined": len(cap),
        "tp1_reach_share": _share_bool(rows, "tp1_reached"), "tp2_reach_share": _share_bool(rows, "tp2_reached"),
        "median_max_structure_reached_r": _median([float(r["max_structure_reached_r"]) for r in rows if r.get("max_structure_reached_r") is not None]),
        "mean_share_bars_above_entry": _mean([r["bars_above_entry"] / (r["bars_above_entry"] + r["bars_below_entry"]) for r in rows if (r.get("bars_above_entry") or 0) + (r.get("bars_below_entry") or 0) > 0]),
        "median_holding_s": _median([float(r["holding_s"]) for r in rows if r.get("holding_s") is not None]),
        "median_signal_age_s": _median([float(r["signal_age_s"]) for r in rows if r.get("signal_age_s") is not None]),
        "spread_over_risk_median": _median([float(r["spread_over_risk"]) for r in rows if r.get("spread_over_risk") is not None]),
        "baseline_exit_shares": {k: sum(1 for r in rows if r.get("baseline_exit") == k) / n for k in sorted({r.get("baseline_exit") for r in rows if r.get("baseline_exit")})},
        "label_counts": {k: labels.count(k) for k in LABELS},
        "label_shares": {k: labels.count(k) / n for k in LABELS},
        "entry_failure_share": failure_share, "useful_entry_share": useful_share,
        "poor_capture_share_of_useful": poor_share,
        "fast_failure_share": sum(1 for r in rows if r.get("fast_failure")) / n,
        "baseline_r_mean": _mean(base), "baseline_r_median": _median(base), "baseline_r_mean_cluster_equal": base_cl,
        "policies": policy_stats,
        "verdict": verdict_for(n=n, n_clusters=n_clusters, failure_share=failure_share, useful_share=useful_share, poor_capture_share_of_useful=poor_share, th=th),
    }
    return out


def _share_bool(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return (sum(1 for v in vals if v) / len(vals)) if vals else None
