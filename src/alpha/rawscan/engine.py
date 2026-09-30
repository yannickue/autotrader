# ruff: noqa: E501
"""Structure-free forced-entry edge scan on a ``DayGrid``.

Trade definition (one trade per cell per day, NO chart-structure logic):
  * decision at the CLOSE of slot s-1, entry at the OPEN of slot s (next open); the only feature is
    the causal ATR(14) known at that open (``DayGrid.ATR``) and, for the conditioned families, a
    signal computed from bars that closed before the entry;
  * exit at the OPEN of slot e = min(s + h, flat slot) (``h = FLAT`` -> the forced-flat slot);
  * prices are BID; cost = half of the recorded spread at entry and at exit x spread_mult, plus one
    slippage per fill (entry, exit), from ``market_costs.cost_scenarios_for`` (BASE and
    COMBINED_ADVERSE); ``EXIT_SHOCK`` = BASE plus one FULL extra spread at every entry and exit;
  * every return is expressed in ATR units; the R variant uses a fixed 1.0 ATR stop (stopped if the
    adverse bar extreme along the path reaches it, booked at exactly -1) otherwise the horizon exit.

A "cell" is (entry slot, horizon, day-sign vector).  Sign vector +1 = LONG, -1 = SHORT, 0 = day not
traded; conditioned families use sign(signal) x family direction; day-of-week slices mask days.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from alpha.rawscan.grid import SLOT_MIN, DayGrid, slot_of
from alpha.rawscan.stats import p_one_sided, t_from_moments

FLAT = -1  # horizon code: until the forced-flat slot
OVERNIGHT = -2  # pseudo horizon: previous cash close -> cash open
HORIZONS: tuple[int, ...] = (3, 6, 12, 24, 48, FLAT)
COST_NAMES = ("BASE", "COMBINED_ADVERSE", "EXIT_SHOCK")
STOP_ATR = 1.0
FIRST30_BARS = 6
LAST60_BARS = 12


def hhmm(slot: int) -> str:
    m = slot * SLOT_MIN
    return f"{m // 60:02d}:{m % 60:02d}"


def hlabel(h: int) -> str:
    return "flat" if h == FLAT else ("ovn" if h == OVERNIGHT else f"{h}b")


@dataclass(frozen=True)
class WindowSpec:
    entry_start: int
    entry_end: int
    flat: int
    cash_open: int
    cash_close: int
    overlap: tuple[int, int] | None = None

    @classmethod
    def from_spec(cls, spec) -> WindowSpec:
        from alpha.fast.sim import SimWindow

        w = SimWindow.from_spec(spec)
        ov = None
        for b in spec.calendar.buckets:
            if b.name.startswith("NY_OVERLAP"):
                ov = (slot_of(b.start_min), slot_of(min(b.end_min, w.flat_min)))
        return cls(
            slot_of(w.entry_start_min), slot_of(w.entry_end_min), slot_of(w.flat_min),
            slot_of(spec.calendar.cash_open_min), slot_of(spec.calendar.cash_close_min), ov,
        )


@dataclass(frozen=True)
class CostParams:
    name: str
    spread_mult: float
    slip_price: float
    extra_spread: float = 0.0  # additional FULL spreads per fill (exit-shock)
    commission_price: float = 0.0  # round-turn commission expressed in price units

    def total(self, sp_in: np.ndarray, sp_out: np.ndarray) -> np.ndarray:
        both = sp_in + sp_out
        return (
            0.5 * self.spread_mult * both + self.extra_spread * both
            + 2.0 * self.slip_price + self.commission_price
        )


def cost_params_for(spec) -> dict[str, CostParams]:
    from alpha.common.market_costs import cost_scenarios_for, eur_per_price_unit_per_lot

    sc = cost_scenarios_for(spec)
    per_unit = eur_per_price_unit_per_lot(spec)
    b, c = sc["BASE"], sc["COMBINED_ADVERSE"]
    return {
        "BASE": CostParams("BASE", b.spread_mult, b.slippage_pts),
        "COMBINED_ADVERSE": CostParams(
            "COMBINED_ADVERSE", c.spread_mult, c.slippage_pts,
            commission_price=c.commission_eur_per_lot / per_unit,
        ),
        "EXIT_SHOCK": CostParams("EXIT_SHOCK", b.spread_mult, b.slippage_pts, extra_spread=1.0),
    }


@dataclass
class Pairs:
    """Stacked (entry slot, horizon) trade streams; all return arrays are ATR units, [D, P]."""

    slot: np.ndarray
    h: np.ndarray
    exit_slot: np.ndarray
    tag: np.ndarray  # object[P]
    in_entry: np.ndarray
    G: np.ndarray  # gross LONG return
    valid: np.ndarray
    cost: dict[str, np.ndarray]
    mfe: np.ndarray  # LONG favourable excursion along the path
    mae: np.ndarray  # LONG adverse excursion
    path_ok: np.ndarray
    n_duplicate_horizons: int = 0

    @property
    def n_pairs(self) -> int:
        return len(self.slot)


def _tag(s: int, win: WindowSpec) -> str:
    if win.cash_open <= s < win.cash_open + FIRST30_BARS:
        return "first30"
    if win.flat - LAST60_BARS <= s < win.flat:
        return "last60"
    if win.overlap and win.overlap[0] <= s < win.overlap[1]:
        return "overlap"
    return "core"


def _stack(lst: list, D: int) -> np.ndarray:
    return np.stack(lst, axis=1) if lst else np.zeros((D, 0))


def build_pairs(
    grid: DayGrid, win: WindowSpec, costs: dict[str, CostParams], horizons=HORIZONS,
    *, include_overnight: bool = True,
) -> Pairs:
    D = grid.n_days
    flat = win.flat
    if not 0 < flat < grid.O.shape[1]:
        raise ValueError("forced-flat slot outside the day grid")
    cols: dict[str, list] = {k: [] for k in ("G", "valid", "mfe", "mae", "path_ok")}
    ccols: dict[str, list] = {n: [] for n in costs}
    slots: list[int] = []
    hs: list[int] = []
    exits: list[int] = []
    tags: list[str] = []
    dup = 0
    for s in range(win.entry_start, flat):
        Os, At, Sp = grid.O[:, s], grid.ATR[:, s], grid.SP[:, s]
        base_ok = np.isfinite(Os) & np.isfinite(At) & (At > 0) & np.isfinite(Sp)
        Hp, Lp = grid.H[:, s:flat], grid.L[:, s:flat]
        fin = np.isfinite(Hp) & np.isfinite(Lp)
        cnt = np.cumsum(fin, axis=1)
        cmax = np.maximum.accumulate(np.where(fin, Hp, -np.inf), axis=1)
        cmin = np.minimum.accumulate(np.where(fin, Lp, np.inf), axis=1)
        seen: set[int] = set()
        for h in horizons:
            e = flat if h == FLAT else min(s + h, flat)
            if e in seen:
                dup += 1
                continue
            seen.add(e)
            j = e - s - 1
            Oe, Se = grid.O[:, e], grid.SP[:, e]
            ok = base_ok & np.isfinite(Oe) & np.isfinite(Se)
            with np.errstate(invalid="ignore", divide="ignore"):
                cols["G"].append((Oe - Os) / At)
                cols["mfe"].append((cmax[:, j] - Os) / At)
                cols["mae"].append((Os - cmin[:, j]) / At)
                for n, cp in costs.items():
                    ccols[n].append(cp.total(Sp, Se) / At)
            cols["valid"].append(ok)
            cols["path_ok"].append(ok & (cnt[:, j] == (e - s)))
            slots.append(s)
            hs.append(int(h))
            exits.append(e)
            tags.append(_tag(s, win))
    if include_overnight and win.cash_close < grid.O.shape[1]:
        co, cc = win.cash_open, win.cash_close
        pv, okp = _prev_row(grid)
        Oe = grid.O[:, co]
        Os = np.where(okp, grid.O[pv, cc], np.nan)
        At = np.where(okp, grid.ATR[pv, cc], np.nan)
        Sp = np.where(okp, grid.SP[pv, cc], np.nan)
        Se = grid.SP[:, co]
        ok = (
            np.isfinite(Os) & np.isfinite(Oe) & np.isfinite(At) & (At > 0)
            & np.isfinite(Sp) & np.isfinite(Se)
        )
        with np.errstate(invalid="ignore", divide="ignore"):
            cols["G"].append((Oe - Os) / At)
            for n, cp in costs.items():
                ccols[n].append(cp.total(Sp, Se) / At)
        cols["mfe"].append(np.full(D, np.nan))
        cols["mae"].append(np.full(D, np.nan))
        cols["valid"].append(ok)
        cols["path_ok"].append(np.zeros(D, dtype=bool))
        slots.append(cc)
        hs.append(OVERNIGHT)
        exits.append(co)
        tags.append("overnight")
    valid = _stack(cols["valid"], D).astype(bool)
    return Pairs(
        slot=np.array(slots, dtype=int), h=np.array(hs, dtype=int),
        exit_slot=np.array(exits, dtype=int), tag=np.array(tags, dtype=object),
        in_entry=np.array(
            [(win.entry_start <= s < win.entry_end) and h != OVERNIGHT
             for s, h in zip(slots, hs, strict=True)], dtype=bool),
        G=np.where(valid, _stack(cols["G"], D), np.nan), valid=valid,
        cost={n: np.where(valid, _stack(v, D), np.nan) for n, v in ccols.items()},
        mfe=_stack(cols["mfe"], D), mae=_stack(cols["mae"], D),
        path_ok=_stack(cols["path_ok"], D).astype(bool), n_duplicate_horizons=dup,
    )


# --------------------------------------------------------------------------- signals / sign vectors
def _prev_row(grid: DayGrid) -> tuple[np.ndarray, np.ndarray]:
    D = grid.n_days
    prev = np.arange(D) - 1
    dd = np.zeros(D)
    dd[1:] = np.diff(grid.dates.astype("int64"))
    ok = (prev >= 0) & (dd <= 4)
    return np.maximum(prev, 0), ok


def day_signals(grid: DayGrid, win: WindowSpec) -> dict[str, np.ndarray]:
    """int8 sign per day (0 = unknown) of causal conditioning signals."""
    co, cc = win.cash_open, win.cash_close
    pv, okp = _prev_row(grid)
    with np.errstate(invalid="ignore"):
        first30 = grid.O[:, co + FIRST30_BARS] - grid.O[:, co]  # known at the open of slot co+6
        gap = np.where(okp, grid.O[:, co] - grid.C[pv, cc - 1], np.nan)  # known at the open
        prevret = np.where(okp, grid.C[pv, cc - 1] - grid.O[pv, co], np.nan)  # prior cash session

    def sg(x):
        return np.nan_to_num(np.sign(x), nan=0.0).astype(np.int8)

    return {"first30": sg(first30), "gap": sg(gap), "prevret": sg(prevret)}


@dataclass
class CellSpace:
    """Cells = (pair column, sign-vector column) with metadata; ``SIG`` is [D, S]."""

    pairs: Pairs
    SIG: np.ndarray
    sig_names: list[str]
    pidx: np.ndarray
    sidx: np.ndarray
    meta: pd.DataFrame
    grid_dow: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def n_cells(self) -> int:
        return len(self.pidx)


def build_cell_space(grid: DayGrid, win: WindowSpec, pairs: Pairs) -> CellSpace:
    D = grid.n_days
    sig = day_signals(grid, win)
    ones = np.ones(D, dtype=np.int8)
    sigmas: list[tuple[str, str, str, np.ndarray, str]] = [
        ("long", "uncond", "L", ones, "all"),
        ("short", "uncond", "S", -ones, "all"),
    ]
    for k in range(5):
        m = (grid.dow == k).astype(np.int8)
        sigmas.append((f"dow{k}_long", "dow", "L", m, "all"))
        sigmas.append((f"dow{k}_short", "dow", "S", -m, "all"))
    sigmas += [
        ("first30_go", "first30_cont", "cond", sig["first30"], "first30"),
        ("first30_fade", "first30_fade", "cond", -sig["first30"], "first30"),
        ("gap_go", "gap_go", "cond", sig["gap"], "gap"),
        ("gap_fade", "gap_fade", "cond", -sig["gap"], "gap"),
        ("ovn_cont", "ovn_cont", "cond", sig["prevret"], "ovn"),
        ("ovn_rev", "ovn_rev", "cond", -sig["prevret"], "ovn"),
    ]
    SIG = np.stack([s[3] for s in sigmas], axis=1).astype(np.int8)
    is_ovn = pairs.h == OVERNIGHT
    co = win.cash_open
    applies = {
        "all": np.ones(pairs.n_pairs, dtype=bool),
        "first30": (pairs.slot == co + FIRST30_BARS) & ~is_ovn,
        "gap": (pairs.slot == co) & ~is_ovn,
        "ovn": is_ovn,
    }
    pidx_l: list[np.ndarray] = []
    sidx_l: list[np.ndarray] = []
    rows: list[pd.DataFrame] = []
    for si, (name, fam, dr, _vec, kind) in enumerate(sigmas):
        cols = np.flatnonzero(applies[kind])
        if not len(cols):
            continue
        pidx_l.append(cols)
        sidx_l.append(np.full(len(cols), si))
        tag = pairs.tag[cols]
        if fam == "uncond":
            family = tag.astype(object)
        elif fam == "dow":
            family = np.array([f"{t}_dow" for t in tag], dtype=object)
        else:
            family = np.array([fam] * len(cols), dtype=object)
        rows.append(pd.DataFrame({
            "family": family, "sigma": name, "dir": dr,
            "slot": pairs.slot[cols], "hhmm": [hhmm(int(s)) for s in pairs.slot[cols]],
            "h": pairs.h[cols], "hlabel": [hlabel(int(h)) for h in pairs.h[cols]],
            "dow": int(name[3]) if fam == "dow" else -1, "tag": tag,
            "in_entry": pairs.in_entry[cols],
            # labelled: NOT invariant to a whole-day shift of the outcome vs the labels
            "labelled": fam != "uncond",
        }))
    meta = pd.concat(rows, ignore_index=True)
    return CellSpace(
        pairs, SIG, [s[0] for s in sigmas], np.concatenate(pidx_l), np.concatenate(sidx_l), meta,
        grid.dow,
    )


# --------------------------------------------------------------------------- cell statistics
def net_matrix(
    cs: CellSpace, cost: str = "BASE", cols: np.ndarray | None = None,
    *, shift: int = 0, stop: bool = False,
) -> np.ndarray:
    """[D, C] net return (ATR units) of the selected cells; NaN where the trade does not exist.

    ``shift`` circularly shifts the OUTCOME arrays (returns, costs, validity) by that many days
    relative to the sign vectors / labels (the day-shift null)."""
    p = cs.pairs
    sel = np.arange(cs.n_cells) if cols is None else cols
    pi, si = cs.pidx[sel], cs.sidx[sel]
    G, V, Cc = p.G[:, pi], p.valid[:, pi], p.cost[cost][:, pi]
    sg = cs.SIG[:, si].astype(float)
    mfe = mae = pok = None
    if stop:
        mfe, mae, pok = p.mfe[:, pi], p.mae[:, pi], p.path_ok[:, pi]
    if shift:
        G, V, Cc = (np.roll(a, shift, axis=0) for a in (G, V, Cc))
        if stop:
            mfe, mae, pok = (np.roll(a, shift, axis=0) for a in (mfe, mae, pok))
    if stop:
        stopped = np.where(sg > 0, mae, mfe) >= STOP_ATR
        gross = np.where(stopped, -STOP_ATR, sg * G)
        V = V & pok
    else:
        gross = sg * G
    return np.where(V & (sg != 0), gross - Cc, np.nan)


def moments(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fin = np.isfinite(X)
    Z = np.where(fin, X, 0.0)
    return fin.sum(0), Z.sum(0), (Z * Z).sum(0)


def _t_by_min_n(n, s1, s2, minn):
    mean = np.full(len(n), np.nan)
    t = np.full(len(n), np.nan)
    for lo in np.unique(minn):
        sel = minn == lo
        mean[sel], t[sel] = t_from_moments(n[sel], s1[sel], s2[sel], int(lo))
    return mean, t


def cell_table(
    cs: CellSpace, day_mask: np.ndarray | None = None, *, min_n: int = 40, min_n_dow: int = 20,
    n_chunks: int = 4,
) -> pd.DataFrame:
    """Observed statistics of every cell over the days in ``day_mask`` (all days if None)."""
    D = cs.pairs.G.shape[0]
    dm = np.ones(D, dtype=bool) if day_mask is None else np.asarray(day_mask, dtype=bool)
    out = cs.meta.copy()
    minn = np.where(out["dow"].to_numpy() >= 0, min_n_dow, min_n)

    def masked(X):
        return np.where(dm[:, None], X, np.nan)

    Xb = masked(net_matrix(cs, "BASE"))
    n, s1, s2 = moments(Xb)
    mean, t = _t_by_min_n(n, s1, s2, minn)
    out["n"] = n
    out["mean_net"] = mean
    out["t"] = t
    out["p1"] = p_one_sided(t, n)
    fin = np.isfinite(Xb)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["hit"] = (fin & (Xb > 0)).sum(0) / n
        win_m = np.where(fin & (Xb > 0), Xb, 0.0).sum(0) / np.maximum((fin & (Xb > 0)).sum(0), 1)
        los_m = -np.where(fin & (Xb < 0), Xb, 0.0).sum(0) / np.maximum((fin & (Xb < 0)).sum(0), 1)
        out["payoff"] = win_m / los_m
    for cn, short in (("COMBINED_ADVERSE", "comb"), ("EXIT_SHOCK", "shock")):
        n2, a1, a2 = moments(masked(net_matrix(cs, cn)))
        out[f"mean_{short}"], out[f"t_{short}"] = _t_by_min_n(n2, a1, a2, minn)
    n3, r1, r2 = moments(masked(net_matrix(cs, "BASE", stop=True)))
    out["mean_R"], out["t_R"] = _t_by_min_n(n3, r1, r2, minn)
    out["n_R"] = n3
    p = cs.pairs
    sg = cs.SIG[:, cs.sidx].astype(float)
    pi = cs.pidx
    ok = dm[:, None] & p.path_ok[:, pi] & (sg != 0)
    mfe = np.where(sg > 0, p.mfe[:, pi], p.mae[:, pi])
    mae = np.where(sg > 0, p.mae[:, pi], p.mfe[:, pi])
    cnt = ok.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mfe_m = np.where(ok, mfe, 0.0).sum(0) / cnt
        mae_m = np.where(ok, mae, 0.0).sum(0) / cnt
        out["mfe"], out["mae"], out["mfe_mae"] = mfe_m, mae_m, mfe_m / mae_m
    chunks = np.array_split(np.flatnonzero(dm), n_chunks)
    cm = []
    for ch in chunks:
        Xc = Xb[ch]
        c = np.isfinite(Xc).sum(0)
        with np.errstate(invalid="ignore", divide="ignore"):
            cm.append(np.where(c >= 3, np.where(np.isfinite(Xc), Xc, 0.0).sum(0) / np.maximum(c, 1), np.nan))
    cm = np.stack(cm, 0)
    sign = np.sign(np.where(np.isfinite(mean), mean, 0.0))
    out["chunks_same_sign"] = (np.sign(cm) == sign[None, :]).sum(0)
    out["chunk_min_mean"] = np.where(np.isfinite(cm), cm, np.inf).min(0)
    out.loc[~np.isfinite(out["chunk_min_mean"]), "chunk_min_mean"] = np.nan
    out["cell_id"] = (
        out["family"].astype(str) + "|" + out["sigma"] + "|" + out["hhmm"] + "|" + out["hlabel"]
    )
    return out
