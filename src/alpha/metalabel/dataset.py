# ruff: noqa: E501
"""Trade-level dataset for meta-labeling: pool specs -> candidates (Train decisions only) -> sim -> features.

SEALING: candidates are subset to Train-side decision bars (``train_mask`` = fold-0 train side) BEFORE the
simulation; every booked trade must have its entry AND exit bar inside the Train mask
(``assert_rows_train_only``).  Nothing here reads a fold TEST mask, the sealed "validation" view of the
evaluator, or any date after ``DEV_END``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from alpha.common.market_data import DEV_END
from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.fast.sim import REASON_TARGET, CandidateArrays, MarketArrays, SimWindow, simulate_fast
from alpha.metalabel import features as F
from alpha.metalabel.evaluate import Outcomes


def assert_rows_train_only(train_mask: np.ndarray, dates: np.ndarray, *bar_idx: np.ndarray) -> None:
    """Every referenced bar lies in the Train mask and on/before the last development day."""
    d = np.asarray(dates).astype("datetime64[D]")
    if len(d) and d.max() > np.datetime64(DEV_END):
        raise RuntimeError("dates after DEV_END reached the meta-label pipeline")
    for idx in bar_idx:
        idx = np.asarray(idx)
        if len(idx) and not train_mask[idx].all():
            raise RuntimeError("a trade row references a bar outside the Train side")
        if len(idx) and d[idx].max() > np.datetime64(DEV_END):
            raise RuntimeError("a trade row references a bar after DEV_END")


def train_cut(dates: np.ndarray, train_mask: np.ndarray) -> int:
    """Number of leading bars up to and including the last Train DAY (bars of later days are never used)."""
    d = np.asarray(dates).astype("datetime64[D]")
    last = d[train_mask].max()
    return int(np.searchsorted(d, last, side="right"))


def truncate_market(m: MarketArrays, n: int) -> MarketArrays:
    return MarketArrays(m.o[:n], m.h[:n], m.l[:n], m.c[:n], m.spread[:n], m.minute[:n], m.day[:n], m.contig_next[:n])


def truncate_inputs(inp: F.FeatureInputs, n: int) -> F.FeatureInputs:
    return F.FeatureInputs({k: np.asarray(v)[:n] for k, v in inp.a.items()}, inp.minute[:n], inp.dow[:n],
                           inp.entry_start_min, inp.entry_end_min)


@dataclass
class SpecCandidates:
    chash: str
    family: str
    source: str  # 'pool_top' | 'extra'
    direction: int
    cands: CandidateArrays  # Train decision bars only (min_space attached)
    zone_lo: np.ndarray
    zone_hi: np.ndarray


@dataclass
class TradeTable:
    spec: np.ndarray  # spec index per row
    decision_idx: np.ndarray
    entry_idx: np.ndarray
    exit_idx: np.ndarray
    direction: np.ndarray
    day: np.ndarray  # entry trading-day ordinal (Train trading days)
    exit_day: np.ndarray
    r: np.ndarray
    y1: np.ndarray
    y3: np.ndarray
    mfe: np.ndarray
    mae: np.ndarray
    hold: np.ndarray
    x: np.ndarray
    names: list[str]
    n_days: int
    source: np.ndarray  # 0 = pool_top, 1 = extra
    meta: dict = field(default_factory=dict)

    def outcomes(self) -> Outcomes:
        return Outcomes(self.r, self.y1, self.y3)

    def __len__(self) -> int:
        return len(self.r)


def candidates_for_specs(entries: list[tuple[str, str, str, object]], frame, resolver, train_mask: np.ndarray,
                         chunk: int = 40) -> list[SpecCandidates]:
    """Evaluate compiled specs on the frame and keep only Train-side decisions.

    ``entries`` = (canonical hash, family name, source, TemporalGenome)."""
    from alpha.discovery import temporal_compile
    from alpha.discovery.temporal_evaluate import attach_min_space
    from alpha.discovery.temporal_genome import canonicalize
    from alpha.temporal.evaluate import evaluate_temporal_many

    out: list[SpecCandidates] = []
    for s in range(0, len(entries), chunk):
        part = entries[s:s + chunk]
        specs = [temporal_compile.compile_temporal(canonicalize(g), resolver, canonical=True) for *_, g in part]
        results = evaluate_temporal_many(specs, frame, use_cache=False)
        for (h, fam, src, g), spec, res in zip(part, specs, results, strict=True):
            c = attach_min_space(res.candidates, spec)
            tm = train_mask[c.decision_idx] if len(c.decision_idx) else np.zeros(0, bool)
            tr = res.trails
            out.append(SpecCandidates(h, fam, src, 1 if g.direction == "LONG" else -1, c.subset(tm),
                                      np.asarray(tr.entry_zone_lo)[tm], np.asarray(tr.entry_zone_hi)[tm]))
    return out


def build_trade_table(pool: list[SpecCandidates], market: MarketArrays, inp: F.FeatureInputs, dates: np.ndarray,
                      train_mask: np.ndarray, cost: CostScenario, sizing: SizingSpec, rules: SimRules,
                      window: SimWindow | None) -> TradeTable:
    """Simulate every pool spec's Train candidates (COMBINED_ADVERSE) and build the feature matrix."""
    d = np.asarray(dates).astype("datetime64[D]")
    train_days = np.unique(d[train_mask])
    day_ord = np.searchsorted(train_days, d)
    # ---- confluence counts over the raw pool candidates (causal: decision bars only)
    rows_dec = np.concatenate([p.cands.decision_idx for p in pool]) if pool else np.zeros(0, dtype=np.int64)
    rows_dir = np.concatenate([p.cands.direction for p in pool]) if pool else np.zeros(0, dtype=np.int8)
    rows_fam = np.concatenate([np.full(len(p.cands.decision_idx), F.family_index(p.family)) for p in pool])
    k_same, k_opp, n_fam = F.confluence_counts(rows_fam, rows_dec, rows_dir)
    off = np.r_[0, np.cumsum([len(p.cands.decision_idx) for p in pool])]
    cols: dict[str, list[np.ndarray]] = {k: [] for k in (
        "spec", "decision", "entry", "exit", "dir", "r", "tgt", "mfe", "mae", "hold", "stop", "target", "target_r",
        "zlo", "zhi", "fam", "ks", "ko", "nf", "src")}
    for si, p in enumerate(pool):
        if len(p.cands.decision_idx) == 0:
            continue
        tr = simulate_fast(market, p.cands, cost, sizing, rules, window)
        if len(tr) == 0:
            continue
        keep = train_mask[tr.entry_idx] & train_mask[tr.exit_idx]
        pos = np.searchsorted(p.cands.decision_idx, tr.decision_idx)
        g = off[si] + pos
        for k, v in (("spec", np.full(len(tr), si)), ("decision", tr.decision_idx), ("entry", tr.entry_idx),
                     ("exit", tr.exit_idx), ("dir", tr.side), ("r", tr.r_multiple),
                     ("tgt", (tr.exit_reason == REASON_TARGET).astype(np.float64)), ("mfe", tr.mfe_r), ("mae", tr.mae_r),
                     ("hold", tr.holding_bars), ("stop", p.cands.stop[pos]), ("target", p.cands.target[pos]),
                     ("target_r", p.cands.target_r[pos]), ("zlo", p.zone_lo[pos]), ("zhi", p.zone_hi[pos]),
                     ("fam", np.full(len(tr), F.family_index(p.family))), ("ks", k_same[g]), ("ko", k_opp[g]),
                     ("nf", n_fam[g]), ("src", np.full(len(tr), 0 if p.source == "pool_top" else 1))):
            cols[k].append(np.asarray(v)[keep])
    cat = {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in cols.items()}
    order = np.lexsort((cat["spec"], cat["decision"], day_ord[cat["entry"].astype(np.int64)] if len(cat["entry"]) else cat["entry"]))
    cat = {k: v[order] for k, v in cat.items()}
    entry, exit_, dec = cat["entry"].astype(np.int64), cat["exit"].astype(np.int64), cat["decision"].astype(np.int64)
    assert_rows_train_only(train_mask, d, dec, entry, exit_)
    ti = F.TradeInfo(dec, cat["dir"].astype(np.float64), cat["stop"], cat["target"], cat["target_r"], cat["zlo"], cat["zhi"],
                     cat["fam"].astype(np.int64), cat["ks"], cat["ko"], cat["nf"])
    raw = F.bar_features(inp)
    x, names = F.trade_matrix(inp, raw, ti)
    r = cat["r"]
    return TradeTable(cat["spec"].astype(np.int64), dec, entry, exit_, cat["dir"].astype(np.int64), day_ord[entry], day_ord[exit_], r,
                      (r > 0).astype(np.float64), cat["tgt"], cat["mfe"], cat["mae"], cat["hold"].astype(np.int64), x, names,
                      len(train_days), cat["src"].astype(np.int64))


__all__ = ("SpecCandidates", "TradeTable", "assert_rows_train_only", "build_trade_table", "candidates_for_specs",
           "train_cut", "truncate_inputs", "truncate_market")
