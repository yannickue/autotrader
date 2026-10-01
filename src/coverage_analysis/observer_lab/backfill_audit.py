# ruff: noqa: E501
"""Audit helpers of the observer backfill (Gate B): leakage / prefix-invariance harness, real-data recomputation, plausibility rules.

OFFLINE / RESEARCH ONLY / OBSERVATION_ONLY. Nothing here changes a feature definition; everything RECOMPUTES features through the public observer
API from truncated or extended bar objects and compares with what the backfill stored.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_observer import bars_adapter as BA
from market_observer.observer import MarketStructureObserver, ObservedEvent, ObserverConfig
from market_observer.schema import ObserverBars

TS_SUFFIX = "_ts_ns"


# ---------------------------------------------------------------------------------------------- equality
def _norm(v: Any) -> Any:
    if isinstance(v, np.generic):
        v = v.item()
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
        return None
    return v


def values_equal(a: Any, b: Any) -> bool:
    """EXACT equality; None and NaN are the same 'unknown'. int 3 == float 3.0 (parquet promotes a column holding NaN to float)."""
    a, b = _norm(a), _norm(b)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) if isinstance(a, bool) and isinstance(b, bool) else False
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    return a == b


def diff_keys(expected: Mapping[str, Any], got: Mapping[str, Any], keys: Sequence[str] | None = None) -> list[str]:
    ks = list(expected) if keys is None else list(keys)
    return [k for k in ks if k not in got or not values_equal(expected[k], got[k])]


# ---------------------------------------------------------------------------------------------- generic prefix-invariance harness
@dataclass
class AuditResult:
    name: str
    n: int = 0
    n_pass: int = 0
    n_fail: int = 0
    n_skipped: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def fail(self, key: Any, detail: Any) -> None:
        self.n += 1
        self.n_fail += 1
        if len(self.failures) < 20:
            self.failures.append({"key": key, "detail": detail})

    def ok(self) -> None:
        self.n += 1
        self.n_pass += 1

    def skip(self, reason: str) -> None:
        self.n_skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1

    @property
    def passed(self) -> bool:
        return self.n_fail == 0 and self.n_pass > 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "n_compared": self.n, "pass": self.n_pass, "fail": self.n_fail, "skipped": self.n_skipped, "skipped_reasons": self.skipped_reasons,
                "failures": self.failures, "verdict": "PASS" if self.passed else "FAIL"}


def prefix_invariance_audit(compute: Callable[[ObserverBars, int], Mapping[str, Any]], bars: ObserverBars, indices: Sequence[int], *, name: str = "prefix_invariance") -> AuditResult:
    """``compute(bars, i)`` on the FULL bars must equal ``compute(bars.prefix(i + 1), i)`` (bars strictly after ``i`` physically removed). A compute
    that needs a bar beyond ``i`` raises ``IndexError`` on the prefix: that is reported as a leak (reads beyond the prefix), never swallowed."""
    res = AuditResult(name)
    for i in indices:
        full = compute(bars, int(i))
        try:
            pre = compute(bars.prefix(int(i) + 1), int(i))
        except IndexError:
            res.fail(int(i), "reads beyond the prefix (IndexError)")
            continue
        bad = diff_keys(full, pre)
        res.ok() if not bad else res.fail(int(i), {k: (full[k], pre[k]) for k in bad[:5]})
    return res


# ---------------------------------------------------------------------------------------------- real-data recomputation
def event_from_row(row: Mapping[str, Any], price: float) -> ObservedEvent:
    return ObservedEvent(
        int(row["direction"]), float(price), family=_norm(row.get("family")), variant=_norm(row.get("variant")),
        structure_event_id=_norm(row.get("m_structure_event_id")), is_control=bool(row.get("is_control", False)), control_of=_norm(row.get("control_of")),
    )


def comparable_keys(stored: Mapping[str, Any], rec_row: Mapping[str, Any], *, with_meta: bool) -> list[str]:
    keys = [k for k in rec_row if k in ("event_id",) or k.startswith(("f_", "v_"))]
    if with_meta:
        keys += [k for k in rec_row if k.startswith("m_") and k in stored]
    else:
        keys += [k for k in ("m_warmup_ok", "m_event_price", "m_reference_level_id") if k in rec_row]
    return keys


def audit_buffer_pass(
    frame: pd.DataFrame, make_buffer: Callable[[], BA.BarBuffer], config: ObserverConfig, point_size: float, stored: pd.DataFrame, price_col: str = "m_event_price",
    *, window: int = 6000, name: str = "live_buffer_prefix",
) -> AuditResult:
    """LIVE-STYLE incremental path: the engine's sliding ``window``-bar frames are synced into an append-only ``BarBuffer`` bar by bar; the observer sees
    ONLY the buffer's physically truncated view (``buf.bars()`` has exactly ``j + 1`` bars at step ``j``). At every sampled row the record must equal the
    stored (batch) one EXACTLY (features, versions, warm-up flag, reference level id, definition hashes, event price). This is at the same time the
    prefix-invariance proof with FULL history and the live-vs-batch parity proof."""
    res = AuditResult(name)
    if stored.empty:
        return res
    arrs = BA.frame_arrays(frame, point_size)
    ts_all = arrs["ts_ns"]
    pos_of_ts = {int(t): k for k, t in enumerate(ts_all)}
    rows_by_idx: dict[int, list[dict[str, Any]]] = {}
    for rec in stored.to_dict("records"):
        k = pos_of_ts.get(int(rec["decision_ts_ns"]) - 300 * 10**9)
        if k is None:
            res.fail(rec.get("event_id"), "decision bar not found in the frame")
            continue
        rows_by_idx.setdefault(k, []).append(rec)
    if not rows_by_idx:
        return res
    last = max(rows_by_idx)
    buf = make_buffer()
    obs = MarketStructureObserver(config)
    for j in range(last + 1):
        lo = max(0, j - window + 1)
        sl = slice(lo, j + 1)
        out = buf.sync(ts_all[sl], arrs["o"][sl], arrs["h"][sl], arrs["low"][sl], arrs["c"][sl], arrs["tick_volume"][sl], arrs["spread"][sl])
        if out == "reset":
            res.fail(j, "BarBuffer reset during the audit pass")
            return res
        bars = buf.bars()
        if len(bars) != j + 1:
            res.fail(j, f"buffer has {len(bars)} bars, expected {j + 1}")
            return res
        obs.advance(bars, j)
        for srow in rows_by_idx.get(j, ()):
            rec = obs.observe(bars, j, event_from_row(srow, float(bars.c[j])))
            got = rec.to_row()
            keys = comparable_keys(srow, got, with_meta=True)
            bad = diff_keys(got, srow, keys)
            if bad:
                res.fail(srow["event_id"], {k: (got.get(k), srow.get(k)) for k in bad[:5]})
            else:
                res.ok()
    return res


def audit_window_recompute(
    frame: pd.DataFrame, build: Callable[[pd.DataFrame], ObserverBars], config: ObserverConfig, stored: pd.DataFrame, *, depth: int | None, extension: int = 0,
    name: str, require_warm: bool = True,
) -> AuditResult:
    """Recompute the record of every stored row from a RAW frame window ``[i - depth + 1, i + extension]`` rebuilt through the batch adapter (``depth=None``:
    from the first bar). ``extension=0`` = truncated exactly at the decision bar; ``extension>0`` = longer (future bars present, the registry is still only
    advanced to the decision bar: arrays built with future bars must not change a value at ``i``). Compared: event id, versions, every ``f_*`` column and
    the warm-up flag. A windowed record that is itself not warm (history shorter than the documented requirement) is SKIPPED when ``require_warm`` is set
    (the documented contract only promises equality once warm); the number of such skips is reported."""
    res = AuditResult(name)
    ts_all = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    pos_of_ts = {int(t): k for k, t in enumerate(ts_all)}
    n = len(frame)
    for srow in stored.to_dict("records"):
        i = pos_of_ts.get(int(srow["decision_ts_ns"]) - 300 * 10**9)
        if i is None:
            res.fail(srow.get("event_id"), "decision bar not found in the frame")
            continue
        s = 0 if depth is None else max(0, i - depth + 1)
        e = min(n, i + 1 + extension)
        bars = build(frame.iloc[s:e].reset_index(drop=True))
        obs = MarketStructureObserver(config)
        obs.advance(bars, i - s)
        rec = obs.observe(bars, i - s, event_from_row(srow, float(bars.c[i - s])))
        got = rec.to_row()
        if require_warm and not bool(rec.meta.get("warmup_ok")) and bool(srow.get("m_warmup_ok")):
            res.skip("windowed_record_not_warm")
            continue
        keys = comparable_keys(srow, got, with_meta=False)
        bad = diff_keys(got, srow, keys)
        res.ok() if not bad else res.fail(srow["event_id"], {k: (got.get(k), srow.get(k)) for k in bad[:5]})
    return res


def audit_shallow_depths(
    frame: pd.DataFrame, build: Callable[[pd.DataFrame], ObserverBars], config: ObserverConfig, stored: pd.DataFrame, depths: Sequence[int] = (120, 240, 500),
) -> dict[str, Any]:
    """Informational + one hard rule: a record observed with a SHORT history (below the documented requirement) must be flagged ``warmup_ok = False``.
    Reports per depth how many were flagged and how many still equal the stored record anyway (not required)."""
    ts_all = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    pos_of_ts = {int(t): k for k, t in enumerate(ts_all)}
    out: dict[str, Any] = {}
    for d in depths:
        n = flagged = equal = bad_flag = 0
        for srow in stored.to_dict("records"):
            i = pos_of_ts.get(int(srow["decision_ts_ns"]) - 300 * 10**9)
            if i is None or i + 1 < d:
                continue
            bars = build(frame.iloc[i - d + 1: i + 1].reset_index(drop=True))
            obs = MarketStructureObserver(config)
            obs.advance(bars, len(bars) - 1)
            rec = obs.observe(bars, len(bars) - 1, event_from_row(srow, float(bars.c[-1])))
            n += 1
            warm = bool(rec.meta.get("warmup_ok"))
            flagged += int(not warm)
            bad_flag += int(warm)  # a record with fewer bars than the requirement claims to be warm -> contract violation
            equal += int(not diff_keys(rec.to_row(), srow, comparable_keys(srow, rec.to_row(), with_meta=False)))
        out[str(d)] = {"n": n, "flagged_not_warm": flagged, "wrongly_flagged_warm": bad_flag, "still_equal_to_stored": equal, "rule_ok": bad_flag == 0}
    return out


# ---------------------------------------------------------------------------------------------- plausibility
_UNIT = re.compile(r"(ratio|efficiency|percentile|close_location)")
_NONNEG = re.compile(
    r"(width|_count$|age_bars|age_minutes|bars_since|sequence_length|baseline_n|tick_activity_per_min|true_range_atr|body_atr|time_held|n_levels|source_count|"
    r"activity_vs|acceleration|max_reentry_depth|tod_baseline_n|midpoint_cross)"
)
_ENUMS: dict[str, tuple[str, ...]] = {}


def _enum_values() -> dict[str, set[str]]:
    from market_observer.schema import LevelRole, LevelSource, SwingLabel, SwingSequence

    return {
        "f_levels__nearest_level_source": {e.value for e in LevelSource}, "f_levels__role": {e.value for e in LevelRole}, "f_levels__previous_role": {e.value for e in LevelRole},
        "f_swings__m5_sequence": {e.value for e in SwingSequence}, "f_swings__m15_sequence": {e.value for e in SwingSequence},
        "f_swings__m5_high_label": {e.value for e in SwingLabel}, "f_swings__m5_low_label": {e.value for e in SwingLabel},
        "f_swings__m15_high_label": {e.value for e in SwingLabel}, "f_swings__m15_low_label": {e.value for e in SwingLabel},
    }


def feature_group_of(col: str) -> str:
    return col.split("__", 1)[0][2:] if col.startswith("f_") and "__" in col else "other"


def plausibility(df: pd.DataFrame, *, tol: float = 1e-9) -> dict[str, Any]:
    """Per feature column: n, missing share, quantiles, constant flag and impossible-value flags (negative widths / counts / ages, ratios outside
    [0, 1], +-inf, timestamps later than the decision time, enum membership). ``df`` = features table (decision features only)."""
    enums = _enum_values()
    cols = [c for c in df.columns if c.startswith("f_")]
    dts = df["decision_ts_ns"].to_numpy(dtype="int64")
    per: dict[str, Any] = {}
    flags: list[dict[str, Any]] = []
    for c in cols:
        s = df[c]
        n = len(s)
        miss = float(s.isna().mean()) if n else float("nan")
        info: dict[str, Any] = {"group": feature_group_of(c), "n": n, "missing_share": round(miss, 6)}
        nn = s.dropna()
        numeric = pd.api.types.is_numeric_dtype(nn) and not pd.api.types.is_bool_dtype(nn)
        info["constant"] = bool(nn.nunique() <= 1) if len(nn) else True
        bad: list[str] = []
        if numeric and len(nn):
            v = nn.to_numpy(float)
            info["quantiles"] = {q: float(np.quantile(v, q)) for q in (0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0)}
            if not np.isfinite(v).all():
                bad.append("non_finite_value")
            if c.endswith(TS_SUFFIX):
                t = s.to_numpy(float)
                later = np.nansum(t > dts.astype(float) + 0.5)
                if later:
                    bad.append(f"timestamp_later_than_decision_time(n={int(later)})")
                if np.nanmin(t) < 0:
                    bad.append("negative_timestamp")
            elif _UNIT.search(c):
                if (v < -tol).any() or (v > 1 + tol).any():
                    bad.append(f"outside_[0,1](min={v.min():.4g},max={v.max():.4g})")
            elif _NONNEG.search(c):
                if (v < -tol).any():
                    bad.append(f"negative(min={v.min():.4g})")
        elif c in enums and len(nn):
            outside = sorted(set(nn.astype(str)) - enums[c])
            info["distinct"] = sorted(set(nn.astype(str)))
            if outside:
                bad.append(f"not_in_enum({outside[:3]})")
        elif len(nn) and not numeric:
            info["distinct"] = sorted(set(nn.astype(str)))[:12]
        if bad:
            info["flags"] = bad
            flags.append({"column": c, "flags": bad})
        per[c] = info
    groups: dict[str, dict[str, Any]] = {}
    for c, info in per.items():
        g = groups.setdefault(info["group"], {"n_columns": 0, "constant_columns": [], "all_missing_columns": [], "high_missing_columns": [], "flagged_columns": 0})
        g["n_columns"] += 1
        if info["constant"]:
            g["constant_columns"].append(c)
        if info["missing_share"] >= 0.999:
            g["all_missing_columns"].append(c)
        elif info["missing_share"] >= 0.5:
            g["high_missing_columns"].append(c)
        g["flagged_columns"] += int("flags" in info)
    return {"columns": per, "groups": groups, "impossible_value_flags": flags, "n_flagged": len(flags)}
