"""Deliberately pessimistic bar-level trade simulator (research only).

Timing: a strategy decides at the CLOSE of bar i; the order fills at the OPEN of bar i+1 (never at
bar i's close). Bars are BID OHLC; the ask side is bid + the bar's recorded spread.

  long  entry : ask open + slippage            long  exit (stop/session): bid - slippage
  short entry : bid open - slippage            short exit (stop/session): ask + slippage
  long  stop  : bid low  <= stop  (gap: bid open <= stop -> fills at the open)
  short stop  : ask high >= stop  (gap: ask open >= stop -> fills at the open)
  long  target: bid high >= target + penetration (limit fill at target)
  short target: ask low  <= target - penetration
When stop and target are both touched inside one bar the STOP wins (order inside a bar is
unknown). The stop is also live in the entry bar. Trailing stops use completed bars only
(the ratcheted level applies from the NEXT bar). No fills across data gaps: a position open when
the next bar is missing is closed at that bar's open (DATA_GAP) and never held overnight
(forced flat at FLAT_MIN Berlin, DAY_END as a backstop).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from alpha.common.frame import (
    ENTRY_END_MIN,
    ENTRY_START_MIN,
    FLAT_MIN,
    Frame,
    session_bucket,
)


@dataclass(frozen=True)
class CostScenario:
    name: str
    spread_mult: float = 1.0
    slippage_pts: float = (
        0.5  # index points (price units), adverse, on every MARKET fill (entry, stop, session)
    )
    target_penetration_pts: float = 0.0
    commission_eur_per_lot: float = 0.0  # ActivTrades GER40 demo: 0 observed


COST_SCENARIOS: dict[str, CostScenario] = {
    "GROSS_REFERENCE": CostScenario("GROSS_REFERENCE", 0.0, 0.0, 0.0),
    "BASE": CostScenario("BASE", 1.0, 0.5, 0.0),
    "SPREAD_STRESS": CostScenario("SPREAD_STRESS", 2.0, 0.5, 0.0),
    "SLIPPAGE_STRESS": CostScenario("SLIPPAGE_STRESS", 1.0, 1.5, 0.0),
    "COMBINED_ADVERSE": CostScenario("COMBINED_ADVERSE", 2.0, 1.5, 1.0),
}


@dataclass(frozen=True)
class SizingSpec:
    """Normalized, comparable sizing: fixed EUR risk per trade, hard research leverage cap."""

    equity_eur: float = 10_000.0
    risk_fraction: float = 0.005  # 1R = 50 EUR
    lot_step: float = 0.25
    min_lot: float = 0.25
    max_leverage: float = 10.0  # research cap, well below the 30x architecture ceiling
    min_risk_pts: float = 5.0
    max_risk_pts: float = 400.0
    contract_size: float = 1.0  # EUR per index point per lot


@dataclass(frozen=True)
class ExitSpec:
    kind: str  # "fixed_r" | "trail"
    r: float  # fixed_r: target multiple of R; trail: trailing distance in R

    @property
    def label(self) -> str:
        return f"{self.kind}{self.r:g}"


@dataclass(frozen=True)
class SimRules:
    max_trades_per_day: int = 6
    max_entry_spread_pts: float = 8.0  # spread hygiene filter (recorded spread, unscaled)


DEFAULT_SIZING = SizingSpec()
DEFAULT_RULES = SimRules()


@dataclass
class Signals:
    """Per-bar decisions made at the CLOSE of the bar: side +1/-1/0, absolute stop price."""

    side: np.ndarray
    stop: np.ndarray
    tag: dict[str, np.ndarray] = field(default_factory=dict)


TRADE_COLUMNS = [
    "decision_idx", "entry_idx", "exit_idx", "side", "entry_ts", "exit_ts", "date",
    "entry_price", "exit_price", "stop_initial", "target", "risk_pts", "qty", "notional_eur",
    "leverage", "pnl_pts", "pnl_eur", "r_multiple", "spread_cost_pts", "slippage_cost_pts",
    "commission_eur", "cost_eur", "gross_pnl_eur", "exit_reason", "mfe_pts", "mae_pts", "mfe_r",
    "mae_r", "bars_to_mfe", "bars_held", "holding_minutes", "entry_spread_pts", "entry_minute",
    "session", "leverage_capped", "crossed_rollover",
]  # fmt: skip


def _floor_step(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


def simulate(
    fr: Frame,
    sig: Signals,
    exit_spec: ExitSpec,
    cost: CostScenario,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Run one strategy variant over the whole frame. Returns (trades, skip counters)."""
    n = len(fr)
    o, h, lo, c, sp = fr.o, fr.h, fr.l, fr.c, fr.spread * cost.spread_mult
    raw_sp_pts = fr.spread
    slip = cost.slippage_pts
    pen = cost.target_penetration_pts
    skips = {
        "no_next_bar": 0, "gap_before_entry": 0, "outside_window": 0, "day_cap": 0,
        "spread_filter": 0, "stop_invalid": 0, "risk_out_of_range": 0, "size_below_min": 0,
        "entry_gap_stop": 0,
    }  # fmt: skip
    rows: list[tuple] = []
    cand = np.flatnonzero(sig.side != 0)
    next_free = 0  # first decision bar allowed (>= exit bar of the previous trade)
    trades_on_day: dict[int, int] = {}
    for i in cand:
        if i < next_free:
            continue
        j = i + 1
        if j >= n:
            skips["no_next_bar"] += 1
            continue
        if not fr.contig_next[i] or fr.day[j] != fr.day[i]:
            skips["gap_before_entry"] += 1
            continue
        if not (ENTRY_START_MIN <= fr.minute[j] < ENTRY_END_MIN):
            skips["outside_window"] += 1
            continue
        if trades_on_day.get(int(fr.day[j]), 0) >= rules.max_trades_per_day:
            skips["day_cap"] += 1
            continue
        entry_spread_pts = max(raw_sp_pts[i], raw_sp_pts[j])
        if entry_spread_pts > rules.max_entry_spread_pts:
            skips["spread_filter"] += 1
            continue
        side = int(sig.side[i])
        stop = float(sig.stop[i])
        e_sp = max(sp[i], sp[j])
        fill = o[j] + e_sp + slip if side > 0 else o[j] - slip
        fill_risk = (fill - stop) if side > 0 else (stop - fill)
        entry_gap_stop = fill_risk <= 0
        risk = abs(c[i] - stop) if entry_gap_stop else fill_risk
        if not np.isfinite(stop):
            skips["stop_invalid"] += 1
            continue
        risk_pts = risk
        if not (sizing.min_risk_pts <= risk_pts <= sizing.max_risk_pts):
            skips["risk_out_of_range"] += 1
            continue
        risk_eur = sizing.equity_eur * sizing.risk_fraction
        qty = _floor_step(risk_eur / (risk * sizing.contract_size), sizing.lot_step)
        capped = False
        max_qty = _floor_step(
            sizing.max_leverage * sizing.equity_eur / (fill * sizing.contract_size), sizing.lot_step
        )
        if qty > max_qty:
            qty, capped = max_qty, True
        if qty < sizing.min_lot:
            skips["size_below_min"] += 1
            continue

        target = (
            (fill + exit_spec.r * risk if side > 0 else fill - exit_spec.r * risk)
            if exit_spec.kind == "fixed_r"
            else float("nan")
        )
        trail_dist = exit_spec.r * risk if exit_spec.kind == "trail" else float("nan")
        cur_stop = stop
        best = (
            h[j] if side > 0 else lo[j] + sp[j]
        )  # extreme in favourable direction (bid high / ask low)
        # MFE/MAE bookkeeping (points, relative to the fill, on the side the position is marked)
        mfe = 0.0
        mae = 0.0
        bars_to_mfe = 0
        exit_idx = j
        exit_px = float("nan")
        reason = ""
        k = j
        while True:
            if entry_gap_stop:
                exit_px = o[j] - slip if side > 0 else o[j] + sp[j] + slip
                exit_idx, reason = j, "ENTRY_GAP_STOP"
                skips["entry_gap_stop"] += 1
                break
            # same-day data gap / forced flat are handled at the OPEN of bar k (k > j only)
            if k > j and not fr.contig_next[k - 1]:
                px = o[k] - slip if side > 0 else o[k] + sp[k] + slip
                exit_idx, exit_px, reason = k, px, "DATA_GAP"
                break
            if fr.minute[k] >= FLAT_MIN:
                px = o[k] - slip if side > 0 else o[k] + sp[k] + slip
                exit_idx, exit_px, reason = k, px, "SESSION_END"
                break
            # stop
            if side > 0:
                if o[k] <= cur_stop:
                    exit_idx, exit_px, reason = k, o[k] - slip, "STOP_GAP"
                elif lo[k] <= cur_stop:
                    exit_idx, exit_px, reason = k, cur_stop - slip, "STOP"
                fav = h[k] - fill
                adv = fill - lo[k]
            else:
                ask_o, ask_h = o[k] + sp[k], h[k] + sp[k]
                if ask_o >= cur_stop:
                    exit_idx, exit_px, reason = k, ask_o + slip, "STOP_GAP"
                elif ask_h >= cur_stop:
                    exit_idx, exit_px, reason = k, cur_stop + slip, "STOP"
                fav = fill - (lo[k] + sp[k])  # short exit is a BUY: marked on the ask low
                adv = ask_h - fill
            if adv > mae:
                mae = adv
            if reason:
                break
            # target (limit) - only if the stop did not trigger in this bar
            if exit_spec.kind == "fixed_r":
                if side > 0 and h[k] >= target + pen:
                    exit_idx, exit_px, reason = k, target, "TARGET"
                    if target - fill > mfe:
                        bars_to_mfe = k - j
                    mfe = target - fill
                    break
                if side < 0 and lo[k] + sp[k] <= target - pen:
                    exit_idx, exit_px, reason = k, target, "TARGET"
                    if fill - target > mfe:
                        bars_to_mfe = k - j
                    mfe = fill - target
                    break
            else:  # ratchet from completed bar k; applies from bar k+1
                if side > 0:
                    best = max(best, h[k])
                    cur_stop = max(cur_stop, best - trail_dist)
                else:
                    best = min(best, lo[k] + sp[k])
                    cur_stop = min(cur_stop, best + trail_dist)
            if fav > mfe:
                mfe, bars_to_mfe = fav, k - j
            # backstop: last bar of the Berlin day (or of the data) -> close, never carry overnight
            if k + 1 >= n or fr.day[k + 1] != fr.day[k]:
                px = c[k] - slip if side > 0 else c[k] + sp[k] + slip
                exit_idx, exit_px, reason = k, px, "DAY_END"
                break
            k += 1

        pnl_pts = (exit_px - fill) if side > 0 else (fill - exit_px)
        # cost decomposition (points): spread is paid on the ask leg only; slippage on market fills
        spread_cost = e_sp if side > 0 else sp[exit_idx]
        n_slip = 1 + (0 if reason == "TARGET" else 1)
        slip_cost = n_slip * slip
        commission = cost.commission_eur_per_lot * qty
        pnl_eur = pnl_pts * qty * sizing.contract_size - commission
        cost_eur = (spread_cost + slip_cost) * qty * sizing.contract_size + commission
        r_mult = pnl_eur / (risk * qty * sizing.contract_size)
        notional = qty * fill * sizing.contract_size
        trades_on_day[int(fr.day[j])] = trades_on_day.get(int(fr.day[j]), 0) + 1
        bars_held = exit_idx - j if reason in {"SESSION_END", "DATA_GAP"} else exit_idx - j + 1
        rows.append(
            (
                int(i), int(j), int(exit_idx), side, fr.ts[j], fr.ts[exit_idx], fr.date[j],
                fill, exit_px, stop, target, risk_pts, qty, notional, notional / sizing.equity_eur,
                pnl_pts, pnl_eur, r_mult, spread_cost, slip_cost, commission, cost_eur,
                pnl_eur + cost_eur, reason, mfe, mae, mfe / risk, mae / risk,
                bars_to_mfe, bars_held, bars_held * 5, entry_spread_pts, int(fr.minute[j]),
                session_bucket(int(fr.minute[j])), capped,
                bool(fr.date[exit_idx] != fr.date[j]),
            )
        )  # fmt: skip
        next_free = exit_idx
    df = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    return df, skips
