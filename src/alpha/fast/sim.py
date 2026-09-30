"""Allocation-light Numba simulator for mass screening.

The market arrays use bid OHLC prices.  Decisions are made at bar close and
filled at the next bar open, matching :mod:`alpha.common.sim`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from numba import njit

from alpha.common.frame import ENTRY_END_MIN, ENTRY_START_MIN, FLAT_MIN
from alpha.common.sim import DEFAULT_RULES, DEFAULT_SIZING, CostScenario, SimRules, SizingSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from alpha.common.frame import Frame
    from alpha.signals import SignalCandidate

EXIT_FIXED_R = np.int8(0)
EXIT_TRAIL = np.int8(1)

REASON_STOP_GAP = np.int8(1)
REASON_STOP = np.int8(2)
REASON_TARGET = np.int8(3)
REASON_SESSION_END = np.int8(4)
REASON_DAY_END = np.int8(5)
REASON_DATA_GAP = np.int8(6)
REASON_ENTRY_GAP_STOP = np.int8(7)

EXIT_REASON_LABELS = {
    int(REASON_STOP_GAP): "STOP_GAP",
    int(REASON_STOP): "STOP",
    int(REASON_TARGET): "TARGET",
    int(REASON_SESSION_END): "SESSION_END",
    int(REASON_DAY_END): "DAY_END",
    int(REASON_DATA_GAP): "DATA_GAP",
    int(REASON_ENTRY_GAP_STOP): "ENTRY_GAP_STOP",
}

SKIP_LABELS = (
    "no_next_bar",
    "gap_before_entry",
    "outside_window",
    "day_cap",
    "spread_filter",
    "stop_invalid",
    "risk_out_of_range",
    "size_below_min",
    "entry_gap_stop",
    # Appended (V2 structural targets): finite target not strictly beyond the fill, or its
    # implied R is not finite / <= 0.  Existing indices 0..8 are unchanged for V1 consumers.
    "target_crossed_at_fill",
)


@dataclass(frozen=True)
class SimWindow:
    """Entry window and forced-flat minute used by :func:`simulate_fast`.

    MINUTE BASIS: identical to ``MarketArrays.minute``, i.e. the minute-of-day of the bar OPEN in
    the market's LOCAL calendar timezone (``MarketSpec.calendar.tz``) as produced by
    ``Frame.from_dataframe(df, params_from_spec(spec))``. The tz conversion happens per bar there
    (DST-correct: 09:30 New York is 09:30 in both EDT and EST), so the window is stated once in
    local wall-clock minutes and needs no per-bar Berlin conversion. ``MarketArrays`` built any
    other way (e.g. Berlin minutes for a New York market) must NOT be paired with a local window.

    ``entry_start_min <= minute[entry bar] < entry_end_min`` admits an entry (else the
    ``outside_window`` skip is counted); a position is closed at the first bar with
    ``minute >= flat_min``. Defaults: the V1 GER40 Berlin constants (09:00-20:00, flat 21:30).
    """

    entry_start_min: int = ENTRY_START_MIN
    entry_end_min: int = ENTRY_END_MIN
    flat_min: int = FLAT_MIN

    def __post_init__(self) -> None:
        if not (0 <= self.entry_start_min < self.entry_end_min <= self.flat_min <= 1440):
            raise ValueError(
                "SimWindow needs 0 <= entry_start < entry_end <= flat <= 1440, got "
                f"{(self.entry_start_min, self.entry_end_min, self.flat_min)}"
            )

    @classmethod
    def from_params(cls, params) -> SimWindow:
        """From ``alpha.common.frame.FrameParams`` (the frame that produced ``minute``)."""
        return cls(params.entry_start_min, params.entry_end_min, params.flat_min)

    @classmethod
    def from_spec(cls, spec) -> SimWindow:
        """From ``markets.spec.MarketSpec.calendar`` (local calendar-tz minutes)."""
        cal = spec.calendar
        return cls(cal.entry_start_min, cal.entry_end_min, cal.forced_flat_min)

    @classmethod
    def from_frame(cls, frame: Frame) -> SimWindow:
        return cls.from_params(frame.params)


GER40_WINDOW = SimWindow()


def _contiguous(array: np.ndarray, dtype: np.dtype) -> np.ndarray:
    return np.ascontiguousarray(array, dtype=dtype)


@dataclass(frozen=True)
class MarketArrays:
    """Immutable-by-convention market inputs required by the fast kernel."""

    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741 - conventional OHLC low name
    c: np.ndarray
    spread: np.ndarray
    minute: np.ndarray
    day: np.ndarray
    contig_next: np.ndarray

    def __post_init__(self) -> None:
        arrays = (
            _contiguous(self.o, np.float64),
            _contiguous(self.h, np.float64),
            _contiguous(self.l, np.float64),
            _contiguous(self.c, np.float64),
            _contiguous(self.spread, np.float64),
            _contiguous(self.minute, np.int64),
            _contiguous(self.day, np.int64),
            _contiguous(self.contig_next, np.bool_),
        )
        if len({len(value) for value in arrays}) != 1:
            raise ValueError("market arrays must have equal length")
        for name, value in zip(self.__dataclass_fields__, arrays, strict=True):
            object.__setattr__(self, name, value)

    @classmethod
    def from_frame(cls, frame: Frame) -> MarketArrays:
        return cls(
            frame.o,
            frame.h,
            frame.l,
            frame.c,
            frame.spread,
            frame.minute,
            frame.day,
            frame.contig_next,
        )


@dataclass(frozen=True)
class CandidateArrays:
    """Sparse decisions; ``target`` overrides the R-derived fixed target when finite."""

    decision_idx: np.ndarray
    direction: np.ndarray
    stop: np.ndarray
    target: np.ndarray
    target_r: np.ndarray
    exit_kind: np.ndarray

    def __post_init__(self) -> None:
        arrays = (
            _contiguous(self.decision_idx, np.int64),
            _contiguous(self.direction, np.int8),
            _contiguous(self.stop, np.float64),
            _contiguous(self.target, np.float64),
            _contiguous(self.target_r, np.float64),
            _contiguous(self.exit_kind, np.int8),
        )
        if len({len(value) for value in arrays}) != 1:
            raise ValueError("candidate arrays must have equal length")
        if len(arrays[0]) and np.any(arrays[0][1:] <= arrays[0][:-1]):
            raise ValueError("decision_idx must be strictly increasing")
        if np.any((arrays[1] != 1) & (arrays[1] != -1)):
            raise ValueError("direction must contain only +1 or -1")
        if np.any((arrays[5] != EXIT_FIXED_R) & (arrays[5] != EXIT_TRAIL)):
            raise ValueError("unknown exit kind")
        for name, value in zip(self.__dataclass_fields__, arrays, strict=True):
            object.__setattr__(self, name, value)

    @classmethod
    def from_signal_candidates(
        cls, frame: Frame, candidates: Sequence[SignalCandidate]
    ) -> CandidateArrays:
        """Map reference candidates while retaining AR1's ExitSpec-derived targets."""
        # signal_ts is the M5 bar CLOSE; the decision bar is the one that OPENED five minutes
        # earlier (same mapping as alpha.signals.evaluation._decision_position).
        decision_open = pd.DatetimeIndex([item.signal_ts for item in candidates]).tz_convert(
            "UTC"
        ) - pd.Timedelta(minutes=5)
        positions = frame.ts.get_indexer(decision_open)
        if np.any(positions < 0):
            raise ValueError("candidate signal_ts does not map to an M5 close in the frame")
        order = np.argsort(positions, kind="stable")
        ordered = [candidates[index] for index in order]
        kinds = np.asarray(
            [EXIT_FIXED_R if item.exit_spec.kind == "fixed_r" else EXIT_TRAIL for item in ordered],
            dtype=np.int8,
        )
        return cls(
            decision_idx=positions[order],
            direction=np.asarray([item.direction for item in ordered], dtype=np.int8),
            stop=np.asarray([item.stop for item in ordered], dtype=np.float64),
            # evaluate_candidates deliberately ignores SignalCandidate.target.
            target=np.full(len(ordered), np.nan),
            target_r=np.asarray([item.exit_spec.r for item in ordered], dtype=np.float64),
            exit_kind=kinds,
        )


@dataclass(frozen=True)
class TradeArrays:
    decision_idx: np.ndarray
    entry_idx: np.ndarray
    exit_idx: np.ndarray
    side: np.ndarray
    entry_day: np.ndarray
    entry_price: np.ndarray
    exit_price: np.ndarray
    risk_pts: np.ndarray
    qty: np.ndarray
    leverage: np.ndarray
    pnl_pts: np.ndarray
    gross_pnl_eur: np.ndarray
    net_pnl_eur: np.ndarray
    r_multiple: np.ndarray
    spread_cost_pts: np.ndarray
    slippage_cost_pts: np.ndarray
    commission_eur: np.ndarray
    cost_eur: np.ndarray
    exit_reason: np.ndarray
    holding_bars: np.ndarray
    mfe_r: np.ndarray
    mae_r: np.ndarray
    entry_gap: np.ndarray
    leverage_capped: np.ndarray
    skip_counts: np.ndarray

    def __len__(self) -> int:
        return len(self.entry_idx)

    @property
    def exit_reason_labels(self) -> np.ndarray:
        labels = [EXIT_REASON_LABELS[int(code)] for code in self.exit_reason]
        return np.asarray(labels, dtype=object)

    @property
    def skips(self) -> dict[str, int]:
        return dict(zip(SKIP_LABELS, (int(value) for value in self.skip_counts), strict=True))

    def as_matrix(self) -> np.ndarray:
        """Numeric matrix useful for deterministic equality checks."""
        if not len(self):
            return np.empty((0, 24), dtype=np.float64)
        names = (name for name in self.__dataclass_fields__ if name != "skip_counts")
        return np.column_stack(
            tuple(getattr(self, name) for name in names)
        )


@njit(cache=True)
def _floor_step(value: float, step: float) -> float:
    return np.floor(value / step + 1e-12) * step


@njit(cache=True)
def _simulate_kernel(
    o: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    raw_spread: np.ndarray,
    minute: np.ndarray,
    day: np.ndarray,
    contig_next: np.ndarray,
    decision_idx: np.ndarray,
    direction: np.ndarray,
    stops: np.ndarray,
    candidate_target: np.ndarray,
    target_r: np.ndarray,
    exit_kind: np.ndarray,
    spread_mult: float,
    slip: float,
    penetration: float,
    commission_per_lot: float,
    equity: float,
    risk_fraction: float,
    lot_step: float,
    min_lot: float,
    max_leverage: float,
    min_risk: float,
    max_risk: float,
    contract_size: float,
    max_trades_per_day: int,
    max_entry_spread: float,
    entry_start_min: int,
    entry_end_min: int,
    flat_min: int,
) -> tuple:
    cap = len(decision_idx)
    out_i = np.empty((5, cap), dtype=np.int64)
    out_f = np.empty((15, cap), dtype=np.float64)
    out_b = np.empty((2, cap), dtype=np.bool_)
    reasons = np.empty(cap, dtype=np.int8)
    skips = np.zeros(10, dtype=np.int64)
    n = len(o)
    count = 0
    next_free = 0
    last_trade_day = -9223372036854775807
    trades_on_day = 0

    for ci in range(cap):
        i = decision_idx[ci]
        if i < next_free:
            continue
        j = i + 1
        if j >= n:
            skips[0] += 1
            continue
        if not contig_next[i] or day[j] != day[i]:
            skips[1] += 1
            continue
        if not (entry_start_min <= minute[j] < entry_end_min):
            skips[2] += 1
            continue
        if day[j] == last_trade_day and trades_on_day >= max_trades_per_day:
            skips[3] += 1
            continue
        entry_spread_pts = max(raw_spread[i], raw_spread[j])
        if entry_spread_pts > max_entry_spread:
            skips[4] += 1
            continue

        side = int(direction[ci])
        stop = stops[ci]
        e_sp = max(raw_spread[i] * spread_mult, raw_spread[j] * spread_mult)
        fill = o[j] + e_sp + slip if side > 0 else o[j] - slip
        fill_risk = fill - stop if side > 0 else stop - fill
        entry_gap = fill_risk <= 0.0
        risk = abs(c[i] - stop) if entry_gap else fill_risk
        if not np.isfinite(stop):
            skips[5] += 1
            continue
        if not (min_risk <= risk <= max_risk):
            skips[6] += 1
            continue
        risk_eur = equity * risk_fraction
        qty = _floor_step(risk_eur / (risk * contract_size), lot_step)
        max_qty = _floor_step(max_leverage * equity / (fill * contract_size), lot_step)
        capped = False
        if qty > max_qty:
            qty = max_qty
            capped = True
        if qty < min_lot:
            skips[7] += 1
            continue

        kind = exit_kind[ci]
        r_value = target_r[ci]
        if kind == EXIT_FIXED_R:
            if np.isfinite(candidate_target[ci]):
                target = candidate_target[ci]
                # Causal guard (finite/structural targets only; NaN target = V1 path untouched):
                # the target must lie strictly beyond the actual fill (spread/slippage included)
                # by more than eps = 1e-9 * max(1, |fill|), and the implied R must be finite > 0.
                # Otherwise skip without consuming the slot / day cap (next_free, trades_on_day
                # and last_trade_day are only updated for booked trades).
                eps = 1e-9 * max(1.0, abs(fill))
                beyond = target - fill if side > 0 else fill - target
                implied_r = beyond / risk
                if not (beyond > eps and np.isfinite(implied_r) and implied_r > 0.0):
                    skips[9] += 1
                    continue
            else:
                target = fill + r_value * risk if side > 0 else fill - r_value * risk
        else:
            target = np.nan
        trail_dist = r_value * risk if kind == EXIT_TRAIL else np.nan
        cur_stop = stop
        best = h[j] if side > 0 else low[j] + raw_spread[j] * spread_mult
        mfe = 0.0
        mae = 0.0
        exit_idx = j
        exit_px = np.nan
        reason = np.int8(0)
        k = j
        while True:
            if entry_gap:
                exit_px = o[j] - slip if side > 0 else o[j] + raw_spread[j] * spread_mult + slip
                exit_idx = j
                reason = REASON_ENTRY_GAP_STOP
                skips[8] += 1
                break
            if k > j and not contig_next[k - 1]:
                exit_px = o[k] - slip if side > 0 else o[k] + raw_spread[k] * spread_mult + slip
                exit_idx = k
                reason = REASON_DATA_GAP
                break
            if minute[k] >= flat_min:
                exit_px = o[k] - slip if side > 0 else o[k] + raw_spread[k] * spread_mult + slip
                exit_idx = k
                reason = REASON_SESSION_END
                break
            if side > 0:
                if o[k] <= cur_stop:
                    exit_idx = k
                    exit_px = o[k] - slip
                    reason = REASON_STOP_GAP
                elif low[k] <= cur_stop:
                    exit_idx = k
                    exit_px = cur_stop - slip
                    reason = REASON_STOP
                fav = h[k] - fill
                adv = fill - low[k]
            else:
                bar_spread = raw_spread[k] * spread_mult
                ask_o = o[k] + bar_spread
                ask_h = h[k] + bar_spread
                if ask_o >= cur_stop:
                    exit_idx = k
                    exit_px = ask_o + slip
                    reason = REASON_STOP_GAP
                elif ask_h >= cur_stop:
                    exit_idx = k
                    exit_px = cur_stop + slip
                    reason = REASON_STOP
                fav = fill - (low[k] + bar_spread)
                adv = ask_h - fill
            if adv > mae:
                mae = adv
            if reason != 0:
                break
            if kind == EXIT_FIXED_R:
                if side > 0 and h[k] >= target + penetration:
                    exit_idx = k
                    exit_px = target
                    reason = REASON_TARGET
                    mfe = target - fill
                    break
                if side < 0 and low[k] + raw_spread[k] * spread_mult <= target - penetration:
                    exit_idx = k
                    exit_px = target
                    reason = REASON_TARGET
                    mfe = fill - target
                    break
            elif side > 0:
                best = max(best, h[k])
                cur_stop = max(cur_stop, best - trail_dist)
            else:
                best = min(best, low[k] + raw_spread[k] * spread_mult)
                cur_stop = min(cur_stop, best + trail_dist)
            if fav > mfe:
                mfe = fav
            if k + 1 >= n or day[k + 1] != day[k]:
                exit_px = c[k] - slip if side > 0 else c[k] + raw_spread[k] * spread_mult + slip
                exit_idx = k
                reason = REASON_DAY_END
                break
            k += 1

        pnl_pts = exit_px - fill if side > 0 else fill - exit_px
        spread_cost = e_sp if side > 0 else raw_spread[exit_idx] * spread_mult
        n_slip = 1 if reason == REASON_TARGET else 2
        slip_cost = n_slip * slip
        commission = commission_per_lot * qty
        net_pnl = pnl_pts * qty * contract_size - commission
        cost_eur = (spread_cost + slip_cost) * qty * contract_size + commission
        gross_pnl = net_pnl + cost_eur
        r_multiple = net_pnl / (risk * qty * contract_size)
        leverage = qty * fill * contract_size / equity
        out_i[0, count] = i
        out_i[1, count] = j
        out_i[2, count] = exit_idx
        out_i[3, count] = side
        out_i[4, count] = day[j]
        out_f[0, count] = fill
        out_f[1, count] = exit_px
        out_f[2, count] = risk
        out_f[3, count] = qty
        out_f[4, count] = leverage
        out_f[5, count] = pnl_pts
        out_f[6, count] = gross_pnl
        out_f[7, count] = net_pnl
        out_f[8, count] = r_multiple
        out_f[9, count] = spread_cost
        out_f[10, count] = slip_cost
        out_f[11, count] = commission
        out_f[12, count] = cost_eur
        out_f[13, count] = mfe / risk
        out_f[14, count] = mae / risk
        reasons[count] = reason
        out_b[0, count] = entry_gap
        out_b[1, count] = capped
        # Reuse an integer row after all calculations to avoid another allocation.
        # This is copied into the TradeArrays holding_bars field by the wrapper.
        # side is already retained above.
        trades_on_day = trades_on_day + 1 if day[j] == last_trade_day else 1
        last_trade_day = day[j]
        next_free = exit_idx
        # Store holding in a dedicated temporary array created lazily via out_i width.
        # The fifth row is entry_day, so append holding through a separate result below.
        count += 1

    holding_out = np.empty(count, dtype=np.int64)
    for index in range(count):
        reason = reasons[index]
        delta = out_i[2, index] - out_i[1, index]
        holding_out[index] = (
            delta if reason in (REASON_SESSION_END, REASON_DATA_GAP) else delta + 1
        )
    return (
        out_i[:, :count],
        out_f[:, :count],
        out_b[:, :count],
        reasons[:count],
        holding_out,
        skips,
    )


def simulate_fast(
    market: MarketArrays,
    candidates: CandidateArrays,
    cost: CostScenario,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
    window: SimWindow | None = None,
) -> TradeArrays:
    """Simulate one sparse candidate stream with AR1-equivalent fills.

    ``window=None`` keeps the V1 GER40 Berlin constants (bit-identical). For any other market pass
    ``SimWindow.from_spec(spec)`` together with ``MarketArrays`` whose ``minute`` is local-calendar
    minutes (see :class:`SimWindow`).
    """
    win = GER40_WINDOW if window is None else window
    if len(candidates.decision_idx) and (
        candidates.decision_idx[0] < 0 or candidates.decision_idx[-1] >= len(market.o)
    ):
        raise IndexError("candidate decision index outside market arrays")
    ints, floats, bools, reasons, holding, skips = _simulate_kernel(
        market.o,
        market.h,
        market.l,
        market.c,
        market.spread,
        market.minute,
        market.day,
        market.contig_next,
        candidates.decision_idx,
        candidates.direction,
        candidates.stop,
        candidates.target,
        candidates.target_r,
        candidates.exit_kind,
        cost.spread_mult,
        cost.slippage_pts,
        cost.target_penetration_pts,
        cost.commission_eur_per_lot,
        sizing.equity_eur,
        sizing.risk_fraction,
        sizing.lot_step,
        sizing.min_lot,
        sizing.max_leverage,
        sizing.min_risk_pts,
        sizing.max_risk_pts,
        sizing.contract_size,
        rules.max_trades_per_day,
        rules.max_entry_spread_pts,
        win.entry_start_min,  # explicit args: numba disk cache cannot keep stale frozen globals
        win.entry_end_min,
        win.flat_min,
    )
    return TradeArrays(
        ints[0], ints[1], ints[2], ints[3].astype(np.int8), ints[4],
        floats[0], floats[1], floats[2], floats[3], floats[4], floats[5], floats[6],
        floats[7], floats[8], floats[9], floats[10], floats[11], floats[12], reasons,
        holding, floats[13], floats[14], bools[0], bools[1], skips,
    )


def simulate_many(
    market: MarketArrays,
    candidate_sets: Sequence[CandidateArrays],
    cost: CostScenario,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
    window: SimWindow | None = None,
) -> list[TradeArrays]:
    """Simulate independent variants; the compiled inner kernel dominates runtime."""
    return [
        simulate_fast(market, candidates, cost, sizing, rules, window)
        for candidates in candidate_sets
    ]


@njit(cache=True)
def _max_consecutive_losses(values: np.ndarray) -> int:
    longest = 0
    current = 0
    for value in values:
        if value < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def day_clustered_mean_ci(
    r_multiple: np.ndarray,
    entry_day: np.ndarray,
    *,
    seed: int = 0,
    n_boot: int = 2000,
    alpha: float = 0.05,
) -> tuple[float | None, float | None]:
    """Seeded day-cluster bootstrap, equivalent to the DataFrame metric helper."""
    r = np.asarray(r_multiple, dtype=float)
    days = np.asarray(entry_day)
    unique_days = np.unique(days)
    if len(unique_days) < 2 or len(r) < 5:
        return None, None
    clusters = [r[days == value] for value in unique_days]
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for index in range(n_boot):
        drawn = rng.integers(0, len(clusters), size=len(clusters))
        means[index] = np.concatenate([clusters[item] for item in drawn]).mean()
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def fast_metrics(
    trades: TradeArrays,
    *,
    trading_days: np.ndarray,
    seed: int = 0,
) -> dict[str, object]:
    """Aggregate screening metrics without constructing a DataFrame."""
    r = trades.r_multiple
    count = len(r)
    n_days = len(np.unique(np.asarray(trading_days)))
    result: dict[str, object] = {
        "trades": count,
        "trading_days": n_days,
        "zero_trade_days": n_days,
        "trades_per_day": count / n_days if n_days else None,
    }
    if not count:
        return result
    wins = r[r > 0]
    losses = r[r < 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    cumulative = np.concatenate((np.asarray([0.0]), np.cumsum(r)))
    drawdown = np.maximum.accumulate(cumulative) - cumulative
    traded_days = len(np.unique(trades.entry_day))
    top_winner_indices = np.flatnonzero(r > 0)
    if len(top_winner_indices):
        remove = top_winner_indices[np.argsort(r[top_winner_indices])[-2:]]
        retained = np.delete(r, remove)
    else:
        retained = r
    result.update(
        win_rate=float((r > 0).mean()),
        expectancy_r=float(r.mean()),
        profit_factor=float(gross_win / gross_loss) if gross_loss > 0 else None,
        avg_winner_r=float(wins.mean()) if len(wins) else None,
        avg_loser_r=float(losses.mean()) if len(losses) else None,
        payoff=float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
        max_drawdown_r=float(drawdown.max()),
        max_consecutive_losses=int(_max_consecutive_losses(r)),
        zero_trade_days=max(0, n_days - traded_days),
        avg_holding_bars=float(trades.holding_bars.mean()),
        median_holding_bars=float(np.median(trades.holding_bars)),
        cost_burden=(
            float(trades.cost_eur.sum() / abs(trades.gross_pnl_eur.sum()))
            if trades.gross_pnl_eur.sum() != 0
            else None
        ),
        expectancy_r_ci95_day_clustered=day_clustered_mean_ci(
            r, trades.entry_day, seed=seed
        ),
        expectancy_without_top_2_winners=(float(retained.mean()) if len(retained) else None),
    )
    return result
