"""FAST <-> NAUTILUS differential (research workbench, OFFLINE ONLY).

Runs the SAME candidates through the existing fast simulator (``alpha.fast.sim.simulate_fast``)
and through the existing Nautilus ``BacktestEngine`` (candidate replay,
``nautilus_kernel.replay_backtest``), normalises both to :class:`NormalizedTrade`, compares every
field and gives EVERY difference a :class:`DiffClass`. There is no second simulator here: the
only code that produces trades is ``simulate_fast`` and Nautilus.

Engine semantics that explain the known differences (measured / read from source):

* FAST fills at the NEXT bar OPEN (``o[j]`` + spread + slippage, long) / ``o[j] - slippage``
  (short); Nautilus' replay fills at the DECISION bar's CLOSE book (ask/bid close, +1 tick if the
  FillModel slips). Identical when ``o[j] == c[i]`` (no gap), otherwise a documented abstraction.
* FAST applies ``cost.slippage_pts`` on every MARKET fill; Nautilus' ``FillModel`` slips exactly
  one tick (0.01) or nothing, on market, stop AND limit fills.
* Within one bar FAST always checks the stop BEFORE the target; Nautilus processes the bar as
  the tick sequence O, H, L, C: long -> target (H) before stop (L), short -> stop (ask-H) before
  target (ask-L).
* All Nautilus ticks of a bar carry the bar CLOSE timestamp; FAST books gap/forced exits at the
  bar OPEN timestamp.

Unknown / unclassified differences => ``status == "FAIL"``; a scenario Nautilus cannot express =>
``status == "BLOCKED"`` with the exact reason; an unexpected exception in the harness / replay =>
``status == "ERROR"`` (type + message in ``summary``). Nothing is ever silently PASS.

SCOPE / WHAT THIS DOES *NOT* VALIDATE (read before citing a PASS)
-----------------------------------------------------------------
The replay is driven by the FAST trades: direction, stop, target, quantity and the signal
timestamp are COPIED into the Nautilus candidates. Comparing them is therefore vacuous BY
CONSTRUCTION; those FieldDiffs carry ``by_construction=True`` / reason ``BY_CONSTRUCTION (not
independent)``, are listed in ``summary["by_construction_fields"]`` and are EXCLUDED from the
matched-field statistics (``class_counts``, ``matched_fields``). The differential validates
EXECUTION semantics (fill, bracket/stop/target priority, gaps, forced exits, slippage/spread
handling, PnL accounting) on IDENTICAL candidates. It does NOT validate candidate generation or
position sizing: sizing correctness of ``simulate_fast`` is asserted separately against hand
calculated numbers (``research_workbench.golden`` expectations + an independent sizing oracle in
the tests). The filled quantity is compared as a guard against fill/rounding defects only.

Lookahead note: the replay submits entries at the decision-bar CLOSE, but the quantity (and a
derived target) come from the FAST trade, i.e. they were computed with the next-bar open as
fill price; forced exits for session end / data gaps are scheduled from the (known) market
arrays exactly as FAST sees them. These are inherited FAST semantics, not new replay lookahead.
"""

from __future__ import annotations

import dataclasses
import math
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from alpha.common.sim import DEFAULT_RULES, DEFAULT_SIZING, CostScenario, SimRules, SizingSpec
from alpha.fast.sim import (
    EXIT_FIXED_R,
    REASON_ENTRY_GAP_STOP,
    CandidateArrays,
    MarketArrays,
    SimWindow,
    simulate_fast,
)

TIMEFRAME = "5m"  # the differential is M5-like (the only timeframe the golden scenarios use)
BAR_NS = 300 * 1_000_000_000
_DAY_NS = 86_400 * 1_000_000_000
# Synthetic calendar origin (a multiple of 86 400 s). Only the ORDER of ``day`` and ``minute``
# matters; ``day`` values are dense-ranked, so any increasing day encoding works.
_EPOCH_BASE_NS = 1_790_380_800 * 1_000_000_000
EXACT_EPS = 1e-9  # relative+absolute equality threshold for "EXACT_MATCH" (float repr noise only)


class DiffClass(StrEnum):
    EXACT_MATCH = "EXACT_MATCH"
    TOLERANCE_MATCH = "TOLERANCE_MATCH"
    EXPECTED_ABSTRACTION = "EXPECTED_ABSTRACTION"
    BROKER_FIDELITY_DIFFERENCE = "BROKER_FIDELITY_DIFFERENCE"
    BUG_SUSPECTED = "BUG_SUSPECTED"


_MATERIAL = frozenset(
    {DiffClass.EXPECTED_ABSTRACTION, DiffClass.BROKER_FIDELITY_DIFFERENCE, DiffClass.BUG_SUSPECTED}
)


@dataclass(frozen=True)
class NormalizedTrade:
    """One schema for both engines."""

    signal_ts_ns: int
    direction: int
    decision_ts_ns: int
    entry_ref: float
    fill_price: float
    stop: float
    target: float | None
    exit_ts_ns: int
    exit_price: float
    exit_reason: str
    r_multiple: float
    gross_pnl: float
    net_pnl: float
    mfe_r: float | None
    mae_r: float | None
    costs: float
    holding_bars: int | None
    qty: float | None = None  # filled quantity (lots)
    # Audit-only (never compared): the engine's real event timestamps. For the Nautilus replay the
    # modeled ts above is the decision / forced-exit bar close, the engine ts is +1 ns (alert
    # orders) or +2 ns (re-entry on the same ts as a forced exit); None for FAST.
    engine_entry_ts_ns: int | None = None
    engine_exit_ts_ns: int | None = None


@dataclass(frozen=True)
class FieldDiff:
    trade_index: int
    field: str
    fast: object
    fidelity: object
    delta: float | None
    diff_class: DiffClass
    reason: str
    leg: str = "JOINT"  # "ENTRY" | "EXIT" | "JOINT" | "TRADE"
    by_construction: bool = False  # replay input echoed from FAST: NOT an independent check


@dataclass(frozen=True)
class DifferentialResult:
    scenario_id: str
    status: str  # "PASS" | "FAIL" | "BLOCKED" | "ERROR"
    fast_trade_count: int
    fidelity_trade_count: int
    field_diffs: tuple[FieldDiff, ...]
    blocked_reason: str | None
    summary: dict[str, Any]


@dataclass(frozen=True)
class Tolerance:
    abs_tol: float
    justification: str
    scale: str = "abs"  # "abs" | "per_lot" (multiplied by the trade quantity)


_HALF_TICK = 0.005 + 1e-9
_PRICE_J = (
    "half the instrument tick (0.01): Nautilus quantises every price to the instrument "
    "price_increment, the FAST arrays are unquantised floats"
)
DEFAULT_TOLERANCES: dict[str, Tolerance] = {
    "signal_ts_ns": Tolerance(0.0, "integer ns on the same bar grid; no tolerance"),
    "decision_ts_ns": Tolerance(0.0, "integer ns on the same bar grid; no tolerance"),
    "exit_ts_ns": Tolerance(0.0, "integer ns on the same bar grid; no tolerance"),
    "direction": Tolerance(0.0, "categorical; no tolerance"),
    "exit_reason": Tolerance(0.0, "categorical; no tolerance"),
    "holding_bars": Tolerance(0.0, "integer bar count; no tolerance"),
    "entry_ref": Tolerance(_HALF_TICK, _PRICE_J),
    "fill_price": Tolerance(_HALF_TICK, _PRICE_J),
    "stop": Tolerance(_HALF_TICK, _PRICE_J),
    "target": Tolerance(_HALF_TICK, _PRICE_J),
    "qty": Tolerance(1e-9, "lot-step grid (0.25); filled quantity must equal the sized quantity"),
    "exit_price": Tolerance(_HALF_TICK, _PRICE_J),
    "r_multiple": Tolerance(
        2e-3, "two half-tick price roundings divided by the minimum research risk of 5 points"
    ),
    "gross_pnl": Tolerance(0.01, "two half-tick roundings (entry+exit) per lot", "per_lot"),
    "net_pnl": Tolerance(0.01, "two half-tick roundings (entry+exit) per lot", "per_lot"),
    "costs": Tolerance(0.01, "two half-tick roundings (entry+exit) per lot", "per_lot"),
    "mfe_r": Tolerance(2e-3, "same as r_multiple"),
    "mae_r": Tolerance(2e-3, "same as r_multiple"),
}

FIELD_LEG: dict[str, str] = {
    "signal_ts_ns": "ENTRY",
    "direction": "ENTRY",
    "decision_ts_ns": "ENTRY",
    "entry_ref": "ENTRY",
    "fill_price": "ENTRY",
    "stop": "ENTRY",
    "target": "ENTRY",
    "qty": "ENTRY",
    "exit_ts_ns": "EXIT",
    "exit_price": "EXIT",
    "exit_reason": "EXIT",
    "r_multiple": "JOINT",
    "gross_pnl": "JOINT",
    "net_pnl": "JOINT",
    "costs": "JOINT",
    "mfe_r": "JOINT",
    "mae_r": "JOINT",
    "holding_bars": "JOINT",
}
_COMPARE_ORDER = tuple(FIELD_LEG)
# Replay inputs copied from the FAST trades: comparing them proves nothing about either engine.
BY_CONSTRUCTION_FIELDS = ("signal_ts_ns", "direction", "stop", "target", "qty")
BY_CONSTRUCTION_NOTE = "BY_CONSTRUCTION (not independent)"
SCOPE_TEXT = (
    "Validates EXECUTION semantics (fill, stop/target priority, gaps, forced exits, slippage and "
    "spread handling, PnL accounting) on IDENTICAL candidates. Direction, stop, target, quantity "
    "and signal timestamp are copied from FAST into the replay (BY_CONSTRUCTION, excluded from "
    "matched-field statistics). Does NOT validate candidate generation or position sizing."
)
NOT_APPLICABLE_FIELDS = {
    "partial_exits": "FAST simulator produces no partial exits",
    "tp1": "FAST simulator has a single fixed target (no TP1)",
    "runner": "FAST simulator has no runner leg",
    "stop_changes": "FAST fixed-R candidates never move the stop (trail kind is BLOCKED)",
    "mfe_r": "Nautilus replay reports no excursion data; excursion analytics live elsewhere",
    "mae_r": "Nautilus replay reports no excursion data; excursion analytics live elsewhere",
}


@dataclass(frozen=True)
class Component:
    """A known, explained contribution to ``fidelity - fast`` for one field."""

    amount: float
    diff_class: DiffClass
    reason: str
    basis: bool = False  # True: moves the bid-basis (gross) price; False: pure cost (spread/slip)


@dataclass(frozen=True)
class PairContext:
    side: int = 1
    qty: float = 1.0
    contract_size: float = 1.0
    components: dict[str, tuple[Component, ...]] = field(default_factory=dict)
    # (fast_reason, fidelity_reason) -> written reason (always EXPECTED_ABSTRACTION)
    reason_alternatives: dict[tuple[str, str], str] = field(default_factory=dict)


# --------------------------------------------------------------------------------- classification


def _tol(tolerances: dict[str, Tolerance], name: str, ctx: PairContext) -> tuple[float, str]:
    t = tolerances[name]
    return (t.abs_tol * (ctx.qty if t.scale == "per_lot" else 1.0), t.justification)


def _explained(comps: tuple[Component, ...]) -> tuple[DiffClass, str]:
    cls = (
        DiffClass.BROKER_FIDELITY_DIFFERENCE
        if any(c.diff_class is DiffClass.BROKER_FIDELITY_DIFFERENCE for c in comps)
        else DiffClass.EXPECTED_ABSTRACTION
    )
    return cls, "; ".join(c.reason for c in comps)


def _derived_components(
    fast: NormalizedTrade, fid: NormalizedTrade, ctx: PairContext
) -> dict[str, tuple[Component, ...]]:
    """Joint-field expectations that FOLLOW from the explained fill/exit price components."""
    legs = [
        c
        for name in ("fill_price", "exit_price")
        for c in ctx.components.get(name, ())
        if c.amount != 0.0
    ]
    if not legs:
        return {}
    cls, why = _explained(tuple(legs))
    note = f"follows from explained price differences ({why})"
    d_fill = sum(c.amount for c in ctx.components.get("fill_price", ()))
    d_exit = sum(c.amount for c in ctx.components.get("exit_price", ()))
    d_fill_b = sum(c.amount for c in ctx.components.get("fill_price", ()) if c.basis)
    d_exit_b = sum(c.amount for c in ctx.components.get("exit_price", ()) if c.basis)
    s, q, cs = ctx.side, ctx.qty, ctx.contract_size
    net_d = s * q * cs * (d_exit - d_fill)
    gross_d = s * q * cs * (d_exit_b - d_fill_b)
    out: dict[str, tuple[Component, ...]] = {
        "net_pnl": (Component(net_d, cls, note),),
        "gross_pnl": (Component(gross_d, cls, note),),
        "costs": (Component(net_d * -1.0 + gross_d, cls, note),),
    }
    # R expectation from PRICE primitives and the shared stop only (never FAST's own pnl / R):
    # r = side * (exit - fill) / |fill - stop|  (commission is 0 here; qty and contract size cancel)
    risk_f = abs(fast.fill_price - fast.stop)
    risk_x = abs(fast.fill_price + d_fill - fast.stop)
    if risk_f > 0 and risk_x > 0:
        r_f = s * (fast.exit_price - fast.fill_price) / risk_f
        r_x = s * ((fast.exit_price + d_exit) - (fast.fill_price + d_fill)) / risk_x
        out["r_multiple"] = (Component(r_x - r_f, cls, note),)
    return out


def _accounting_diffs(
    trade_index: int,
    who: str,
    t: NormalizedTrade,
    ctx: PairContext,
    tolerances: dict[str, Tolerance],
) -> list[FieldDiff]:
    """Is ONE engine's net / R internally consistent with ITS OWN fill, exit, qty, stop?

    A coherently wrong accounting (e.g. net scaled by 1.1) can never be explained away by price
    components: it is flagged here as BUG_SUSPECTED regardless of the other engine.
    """
    out: list[FieldDiff] = []
    q = t.qty if t.qty is not None else ctx.qty
    s, cs = t.direction, ctx.contract_size
    tol_net = tolerances["net_pnl"].abs_tol * q
    exp_net = s * q * cs * (t.exit_price - t.fill_price)
    if abs(t.net_pnl - exp_net) > max(tol_net, EXACT_EPS):
        out.append(
            FieldDiff(
                trade_index,
                f"{who}_net_pnl_consistency",
                t.net_pnl,
                exp_net,
                t.net_pnl - exp_net,
                DiffClass.BUG_SUSPECTED,
                f"{who} net_pnl inconsistent with its own fill/exit/qty",
                "JOINT",
            )
        )
    risk = abs(t.fill_price - t.stop)
    if risk > 0:
        exp_r = s * (t.exit_price - t.fill_price) / risk
        if abs(t.r_multiple - exp_r) > max(tolerances["r_multiple"].abs_tol, EXACT_EPS):
            out.append(
                FieldDiff(
                    trade_index,
                    f"{who}_r_multiple_consistency",
                    t.r_multiple,
                    exp_r,
                    t.r_multiple - exp_r,
                    DiffClass.BUG_SUSPECTED,
                    f"{who} r_multiple inconsistent with its own fill/exit/stop",
                    "JOINT",
                )
            )
    return out


def classify_pair(
    trade_index: int,
    fast: NormalizedTrade,
    fid: NormalizedTrade,
    ctx: PairContext | None = None,
    tolerances: dict[str, Tolerance] = DEFAULT_TOLERANCES,
) -> list[FieldDiff]:
    """Compare one matched trade pair field by field and classify EVERY field (pure function)."""
    ctx = ctx or PairContext(side=fast.direction)
    derived = _derived_components(fast, fid, ctx)
    diffs: list[FieldDiff] = []
    diffs.extend(_accounting_diffs(trade_index, "fast", fast, ctx, tolerances))
    diffs.extend(_accounting_diffs(trade_index, "fidelity", fid, ctx, tolerances))
    for name in _COMPARE_ORDER:
        fv, xv = getattr(fast, name), getattr(fid, name)
        leg = FIELD_LEG[name]
        if name in ("mfe_r", "mae_r") and (fv is None or xv is None):
            continue  # not applicable (reported in the summary), never counted as a match
        if name == "qty" and fv is None and xv is None:
            continue  # quantity not provided (hand-built pairs)
        if name == "target" and fv is None and xv is None:
            diffs.append(FieldDiff(trade_index, name, fv, xv, 0.0, DiffClass.EXACT_MATCH, "", leg))
            continue
        if fv is None or xv is None:
            diffs.append(
                FieldDiff(
                    trade_index,
                    name,
                    fv,
                    xv,
                    None,
                    DiffClass.BUG_SUSPECTED,
                    "one side missing",
                    leg,
                )
            )
            continue
        if isinstance(fv, str):
            if fv == xv:
                cls, why = DiffClass.EXACT_MATCH, ""
            elif (fv, xv) in ctx.reason_alternatives:
                cls, why = DiffClass.EXPECTED_ABSTRACTION, ctx.reason_alternatives[(fv, xv)]
            else:
                cls, why = DiffClass.BUG_SUSPECTED, f"unexplained {name}: {fv!r} vs {xv!r}"
            diffs.append(FieldDiff(trade_index, name, fv, xv, None, cls, why, leg))
            continue
        delta = float(xv) - float(fv)
        tol, tol_why = _tol(tolerances, name, ctx)
        comps = tuple(
            c for c in (*ctx.components.get(name, ()), *derived.get(name, ())) if c.amount != 0.0
        )
        if abs(delta) <= EXACT_EPS * max(1.0, abs(float(fv))) and not comps:
            cls, why = DiffClass.EXACT_MATCH, ""
        elif comps and abs(delta - sum(c.amount for c in comps)) <= max(tol, EXACT_EPS):
            cls, why = _explained(comps)
        elif abs(delta) <= EXACT_EPS * max(1.0, abs(float(fv))):
            cls, why = DiffClass.EXACT_MATCH, ""
        elif abs(delta) <= tol:
            cls, why = DiffClass.TOLERANCE_MATCH, tol_why
        else:
            cls, why = DiffClass.BUG_SUSPECTED, f"unexplained delta {delta!r} (tolerance {tol!r})"
        diffs.append(FieldDiff(trade_index, name, fv, xv, delta, cls, why, leg))
    return [_mark_by_construction(d) for d in diffs]


def _mark_by_construction(d: FieldDiff) -> FieldDiff:
    if d.field not in BY_CONSTRUCTION_FIELDS:
        return d
    reason = BY_CONSTRUCTION_NOTE + (f": {d.reason}" if d.reason else "")
    return dataclasses.replace(d, reason=reason, by_construction=True)


def trade_mismatch_leg(diffs: list[FieldDiff], *, strict: bool = False) -> str:
    """ENTRY_MISMATCH / EXIT_MISMATCH / BOTH / NONE for one trade's diffs.

    Default: a leg mismatches when it has any EXPECTED_ABSTRACTION / BROKER_FIDELITY_DIFFERENCE /
    BUG_SUSPECTED field. ``strict=True`` counts every non-EXACT_MATCH field (incl. tolerance).
    JOINT (derived) fields never decide the leg; they follow from the legs.
    """
    bad = (
        (lambda d: d.diff_class is not DiffClass.EXACT_MATCH)
        if strict
        else (lambda d: d.diff_class in _MATERIAL)
    )
    entry = any(d.leg == "ENTRY" and bad(d) for d in diffs)
    exit_ = any(d.leg == "EXIT" and bad(d) for d in diffs)
    return (
        "BOTH"
        if entry and exit_
        else "ENTRY_MISMATCH"
        if entry
        else "EXIT_MISMATCH"
        if exit_
        else "NONE"
    )


# ------------------------------------------------------------------------------------ timestamps


def netting_report(trades: Any, candidates: CandidateArrays) -> dict[str, Any]:
    """One-position-at-a-time (NETTING account) audit of the FAST output, independent of the sim.

    ``simulate_fast`` admits a candidate only if ``decision_idx >= next_free`` (previous exit_idx)
    (``alpha/fast/sim.py:343`` / ``:534``): a decision at the
    CLOSE of the exit bar is allowed (the exit happened inside that bar or at its open, the new
    entry fills at the NEXT open), a decision before the exit bar is silently dropped (not in
    ``TradeArrays.skips``). So FAST is already one-position-at-a-time. This audit (a) re-verifies
    that no FAST trade overlaps the previous one (``overlap_violations``; > 0 would mean FAST
    admits positions a netting account cannot hold -> BUG_SUSPECTED ``FAST_ALLOWS_OVERLAP``) and
    (b) counts the candidates FAST dropped because a position was open
    (``candidates_blocked_by_open_position``), which is invisible in ``skips``.
    """
    n = len(trades.decision_idx)
    dec = [int(x) for x in trades.decision_idx]
    ent = [int(x) for x in trades.entry_idx]
    ext = [int(x) for x in trades.exit_idx]
    # All execution intervals independently: sort by entry, track the max exit seen so far, so
    # unsorted output and NESTED intervals are caught (not just adjacent pairs).
    order = sorted(range(n), key=lambda k: (ent[k], dec[k]))
    violations: list[int] = []
    max_exit = None
    for k in order:
        if max_exit is not None and (dec[k] < max_exit or ent[k] <= max_exit):
            violations.append(k)
        max_exit = ext[k] if max_exit is None else max(max_exit, ext[k])
    violations.sort()
    # Candidate identity by multiplicity: a candidate index shared by several candidates is only
    # "traded" as many times as trades exist with that decision index.
    from collections import Counter

    cand_n = Counter(int(x) for x in candidates.decision_idx)
    exec_n = Counter(dec)
    blocked = 0
    other_unfilled = 0
    for i, m in cand_n.items():
        for _ in range(m - exec_n.get(i, 0)):
            if any(dec[k] < i < ext[k] for k in range(n)):
                blocked += 1
            else:
                other_unfilled += 1
    return {
        "fast_trades": n,
        "overlap_violations": len(violations),
        "overlap_trade_indices": violations,
        "same_bar_reentries": sum(1 for k in range(1, n) if dec[k] == ext[k - 1]),
        "candidates_total": len(candidates.decision_idx),
        "candidates_blocked_by_open_position": blocked,
        "candidates_unfilled_other": other_unfilled,
        "duplicate_decision_indices": sum(1 for m in cand_n.values() if m > 1),
        "rule": "FAST admits decision_idx >= previous exit_idx (one position at a time)",
    }


def bar_open_ts_ns(market: MarketArrays) -> np.ndarray:
    """Synthetic increasing bar OPEN timestamps from ``(day, minute)`` (dense-ranked day)."""
    _, rank = np.unique(market.day, return_inverse=True)
    ts = (
        _EPOCH_BASE_NS
        + rank.astype(np.int64) * _DAY_NS
        + market.minute.astype(np.int64) * 60_000_000_000
    )
    if len(ts) > 1 and np.any(ts[1:] <= ts[:-1]):
        raise ValueError("(day, minute) must be strictly increasing to build bar timestamps")
    return ts


# ---------------------------------------------------------------------------------- FAST normaliser


def normalize_fast(
    market: MarketArrays, candidates: CandidateArrays, trades: Any
) -> list[NormalizedTrade]:
    """``TradeArrays`` -> NormalizedTrade list. Timestamps: bar-OPEN grid + 5 min for closes."""
    open_ts = bar_open_ts_ns(market)
    cand_row = {int(d): n for n, d in enumerate(candidates.decision_idx)}
    out: list[NormalizedTrade] = []
    for n in range(len(trades)):
        i, j, k = int(trades.decision_idx[n]), int(trades.entry_idx[n]), int(trades.exit_idx[n])
        c = cand_row[i]
        side = int(trades.side[n])
        reason = str(trades.exit_reason_labels[n])
        fill = float(trades.entry_price[n])
        risk = float(trades.risk_pts[n])
        target: float | None
        if int(candidates.exit_kind[c]) != int(EXIT_FIXED_R):
            target = None
        elif math.isfinite(float(candidates.target[c])):
            target = float(candidates.target[c])
        else:
            target = fill + side * float(candidates.target_r[c]) * risk
        at_open = reason in ("SESSION_END", "DATA_GAP", "STOP_GAP", "ENTRY_GAP_STOP")
        exit_ts = int(open_ts[k] if at_open else open_ts[k] + BAR_NS)
        sig = int(open_ts[i] + BAR_NS)
        out.append(
            NormalizedTrade(
                signal_ts_ns=sig,
                direction=side,
                decision_ts_ns=sig,
                entry_ref=float(market.o[j]),
                fill_price=fill,
                stop=float(candidates.stop[c]),
                target=target,
                exit_ts_ns=exit_ts,
                exit_price=float(trades.exit_price[n]),
                exit_reason=reason,
                r_multiple=float(trades.r_multiple[n]),
                gross_pnl=float(trades.gross_pnl_eur[n]),
                net_pnl=float(trades.net_pnl_eur[n]),
                mfe_r=float(trades.mfe_r[n]),
                mae_r=float(trades.mae_r[n]),
                costs=float(trades.cost_eur[n]),
                holding_bars=int(trades.holding_bars[n]),
                qty=float(trades.qty[n]),
            )
        )
    return out


# ------------------------------------------------------------------------------ Nautilus side


def forced_exit_for(market: MarketArrays, window: SimWindow, j: int) -> tuple[int, str]:
    """Bar index whose CLOSE ts the replay flattens at, and the reason (FAST rule order).

    Mirrors the FAST scan order at each bar ``k >= j``: data gap, forced-flat minute, day end.
    Gap/flat exits are decided at the CLOSE of bar ``k - 1`` (== OPEN of ``k``); day end at the
    close of bar ``k``.
    """
    n = len(market.o)
    for k in range(j, n):
        if k > j and not market.contig_next[k - 1]:
            return k - 1, "DATA_GAP"
        if int(market.minute[k]) >= window.flat_min:
            return max(k - 1, 0), "SESSION_END"
        if k + 1 >= n or market.day[k + 1] != market.day[k]:
            return k, "DAY_END"
    return n - 1, "DAY_END"


def _default_instrument() -> Any:
    from nautilus_kernel.instrument import load_symbol_info_snapshot, spec_to_nautilus_cfd

    path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ger40" / "symbol_info.json"
    if not path.is_file():
        raise FileNotFoundError(f"GER40 symbol_info fixture not found: {path}")
    return spec_to_nautilus_cfd(load_symbol_info_snapshot(path).spec)


def _write_synthetic_catalog(
    path: Path, instrument: Any, market: MarketArrays, spread_mult: float, open_ts: np.ndarray
) -> None:
    from nautilus_trader.model.data import Bar, BarType

    from nautilus_kernel.catalog import bar_type_strings, write_catalog

    bid_s, ask_s = bar_type_strings(instrument, TIMEFRAME)
    bid_t, ask_t = BarType.from_str(bid_s), BarType.from_str(ask_s)
    qty = instrument.make_qty(100.0)
    bids: list[Any] = []
    asks: list[Any] = []
    for k in range(len(market.o)):
        ts = int(open_ts[k]) + BAR_NS
        sp = float(market.spread[k]) * spread_mult
        for tgt, bt, shift in ((bids, bid_t, 0.0), (asks, ask_t, sp)):
            tgt.append(
                Bar(
                    bt,
                    instrument.make_price(float(market.o[k]) + shift),
                    instrument.make_price(float(market.h[k]) + shift),
                    instrument.make_price(float(market.l[k]) + shift),
                    instrument.make_price(float(market.c[k]) + shift),
                    qty,
                    ts,
                    ts,
                )
            )
    write_catalog(path, instrument, bids, asks)


def normalize_replay(
    replay: Any,
    market: MarketArrays,
    cost: CostScenario,
    tick: float,
    open_ts: np.ndarray,
) -> list[NormalizedTrade]:
    """Pair entry/exit fills of the Nautilus replay into NormalizedTrades.

    ``net_pnl`` is Nautilus' own realised PnL. ``gross_pnl`` is the bid-basis price move (same
    definition as FAST: spread and slippage removed), ``costs = gross - net``; the spread/slippage
    removed are the book spread at the fill bar and the FillModel tick actually configured.
    """
    slip_x = tick if cost.slippage_pts > 0 else 0.0
    close_ts = open_ts + BAR_NS
    bar_of = {int(t): n for n, t in enumerate(close_ts)}
    sm = cost.spread_mult
    fills = list(replay.fills)
    trades: list[NormalizedTrade] = []
    p = 0
    for decision, pnl in zip(replay.decisions, replay.realized_pnl, strict=False):
        cand, submit_ts = decision
        while p < len(fills) and fills[p].tag != "entry":
            p += 1
        if p >= len(fills):
            break
        entry = fills[p]
        exit_ = fills[p + 1] if p + 1 < len(fills) and fills[p + 1].tag != "entry" else None
        p += 2
        if exit_ is None:
            continue
        side = 1 if entry.side == "BUY" else -1
        i = bar_of[int(cand.decision_ts_ns)]
        kx = bar_of[int(exit_.ts_ns)]
        qty = entry.qty
        reason = exit_.tag.split(":", 1)[1] if ":" in exit_.tag else exit_.tag
        if reason == "STOP":
            worse = (cand.stop - exit_.price) if side > 0 else (exit_.price - cand.stop)
            if worse > slip_x + tick / 2:
                reason = "STOP_GAP"
        # bid-basis prices (remove the book spread and the FillModel slippage)
        entry_basis = entry.price - side * slip_x - (market.spread[i] * sm if side > 0 else 0.0)
        exit_basis = exit_.price + side * slip_x - (market.spread[kx] * sm if side < 0 else 0.0)
        gross = side * qty * (exit_basis - entry_basis)
        net = float(pnl)
        risk = abs(entry.price - cand.stop)
        trades.append(
            NormalizedTrade(
                signal_ts_ns=int(cand.decision_ts_ns),
                direction=side,
                decision_ts_ns=int(submit_ts),
                entry_ref=float(market.c[i]),
                fill_price=entry.price,
                stop=cand.stop,
                target=cand.target,
                exit_ts_ns=int(exit_.ts_ns),
                exit_price=exit_.price,
                exit_reason=reason,
                r_multiple=net / (risk * qty) if risk > 0 else float("nan"),
                gross_pnl=gross,
                net_pnl=net,
                mfe_r=None,
                mae_r=None,
                costs=gross - net,
                holding_bars=int((exit_.ts_ns - entry.ts_ns) // BAR_NS),
                qty=float(qty),
                engine_entry_ts_ns=int(entry.engine_ts_ns),
                engine_exit_ts_ns=int(exit_.engine_ts_ns),
            )
        )
    return trades


# ------------------------------------------------------------------------- context construction


def _build_context(
    fast: NormalizedTrade,
    fid: NormalizedTrade,
    market: MarketArrays,
    cost: CostScenario,
    tick: float,
    open_ts: np.ndarray,
    qty: float,
    fast_exit_idx: int,
    fast_entry_idx: int,
    fast_decision_idx: int,
) -> PairContext:
    side = fast.direction
    sm = cost.spread_mult
    slip_f = cost.slippage_pts
    slip_x = tick if cost.slippage_pts > 0 else 0.0
    i, j, k = fast_decision_idx, fast_entry_idx, fast_exit_idx
    comps: dict[str, list[Component]] = {
        n: [] for n in ("entry_ref", "fill_price", "exit_price", "exit_ts_ns")
    }
    alts: dict[tuple[str, str], str] = {}

    # --- ENTRY -------------------------------------------------------------------------------
    gap = float(market.c[i]) - float(market.o[j])
    gap_reason = (
        "fill-timing abstraction: FAST fills at the next bar OPEN o[j], the Nautilus replay at the "
        "decision bar CLOSE c[i] (they differ only across a price gap)"
    )
    comps["entry_ref"].append(Component(gap, DiffClass.EXPECTED_ABSTRACTION, gap_reason, True))
    comps["fill_price"].append(Component(gap, DiffClass.EXPECTED_ABSTRACTION, gap_reason, True))
    if side > 0:
        sp_f = max(float(market.spread[i]), float(market.spread[j])) * sm
        sp_x = float(market.spread[i]) * sm
        comps["fill_price"].append(
            Component(
                sp_x - sp_f,
                DiffClass.EXPECTED_ABSTRACTION,
                "FAST pays max(spread[i], spread[j]); Nautilus pays the decision-bar book spread",
            )
        )
    comps["fill_price"].append(
        Component(
            side * (slip_x - slip_f),
            DiffClass.BROKER_FIDELITY_DIFFERENCE,
            f"entry slippage: FAST cost.slippage_pts={slip_f}, Nautilus FillModel slips "
            f"{slip_x} (exactly one tick or none)",
        )
    )

    # --- EXIT --------------------------------------------------------------------------------
    reason = fast.exit_reason
    exit_slip_f = 0.0 if reason == "TARGET" else slip_f
    # Nautilus slips every fill (market, stop and limit) when the FillModel slips.
    comps["exit_price"].append(
        Component(
            -side * (slip_x - exit_slip_f),
            DiffClass.BROKER_FIDELITY_DIFFERENCE,
            f"exit slippage: FAST applies {exit_slip_f} on a {reason} exit, Nautilus FillModel "
            f"slips {slip_x} on every fill incl. limit (target) fills",
        )
    )
    open_k = int(open_ts[k])
    if reason in ("SESSION_END", "DATA_GAP"):
        kb = max(k - 1, 0)
        comps["exit_price"].append(
            Component(
                float(market.c[kb]) - float(market.o[k]),
                DiffClass.EXPECTED_ABSTRACTION,
                f"{reason} exit-timing abstraction: FAST exits at the OPEN of bar k, the replay at "
                "the CLOSE of bar k-1 (differ across a data/price gap)",
                True,
            )
        )
        if side < 0:
            comps["exit_price"].append(
                Component(
                    (float(market.spread[kb]) - float(market.spread[k])) * sm,
                    DiffClass.EXPECTED_ABSTRACTION,
                    f"{reason}: short exit pays the ask spread of bar k-1 (replay) vs bar k (FAST)",
                )
            )
        comps["exit_ts_ns"].append(
            Component(
                float(int(open_ts[kb]) + BAR_NS - open_k),
                DiffClass.EXPECTED_ABSTRACTION,
                f"{reason} exit-timing abstraction: FAST stamps the OPEN of bar k, the replay the "
                "close of bar k-1",
            )
        )
    elif reason == "STOP_GAP":
        comps["exit_ts_ns"].append(
            Component(
                float(BAR_NS),
                DiffClass.EXPECTED_ABSTRACTION,
                "STOP_GAP: FAST stamps the bar OPEN where it gapped through the stop; every "
                "Nautilus tick of a bar carries the bar CLOSE ts_event",
            )
        )
    # --- intra-bar ordering (stop vs target in the same bar) ---------------------------------
    tgt, stp = fast.target, fast.stop
    if tgt is not None and fid.exit_reason != fast.exit_reason:
        kb_hit = k
        sm_k = float(market.spread[kb_hit]) * sm
        if side > 0:
            both = float(market.h[kb_hit]) >= tgt and float(market.l[kb_hit]) <= stp
        else:
            both = float(market.l[kb_hit]) + sm_k <= tgt and float(market.h[kb_hit]) + sm_k >= stp
        if both:
            why = (
                "intrabar-order abstraction: bar touches BOTH stop and target; FAST always "
                "checks the stop first, Nautilus walks the ticks O,H,L,C "
                "(long: target first, short: stop first)"
            )
            alts[(fast.exit_reason, fid.exit_reason)] = why
            alt_px = tgt if fid.exit_reason == "TARGET" else stp
            fast_pre_slip = fast.exit_price + side * exit_slip_f  # FAST exit before its slippage
            comps["exit_price"].append(
                Component(alt_px - fast_pre_slip, DiffClass.EXPECTED_ABSTRACTION, why, True)
            )
    return PairContext(
        side=side,
        qty=qty,
        components={n: tuple(v) for n, v in comps.items()},
        reason_alternatives=alts,
    )


# ------------------------------------------------------------------------------------ entry point


def _netting_diffs(netting: dict[str, Any]) -> list[FieldDiff]:
    if not netting["overlap_violations"]:
        return []
    return [
        FieldDiff(
            idx, "FAST_ALLOWS_OVERLAP", "overlapping entry", "one position (netting)", None,
            DiffClass.BUG_SUSPECTED,
            "FAST trade starts before the previous trade exited: a NETTING account (one "
            "position per instrument) cannot hold it; counts and expectancy are not realisable",
            "TRADE",
        )
        for idx in netting["overlap_trade_indices"]
    ]  # fmt: skip


def _blocked(
    scenario_id: str, fast_n: int, reason: str, extra: dict[str, Any] | None = None
) -> DifferentialResult:
    return DifferentialResult(
        scenario_id=scenario_id,
        status="BLOCKED",
        fast_trade_count=fast_n,
        fidelity_trade_count=0,
        field_diffs=(),
        blocked_reason=reason,
        summary={
            "not_applicable": dict(NOT_APPLICABLE_FIELDS),
            "by_construction_fields": list(BY_CONSTRUCTION_FIELDS),
            "scope": SCOPE_TEXT,
            **(extra or {}),
        },
    )


def _error(scenario_id: str, fast_n: int, where: str, exc: BaseException) -> DifferentialResult:
    """Unexpected harness / replay exception: NOT 'unsupported' (BLOCKED) but a defect."""
    return DifferentialResult(
        scenario_id=scenario_id,
        status="ERROR",
        fast_trade_count=fast_n,
        fidelity_trade_count=0,
        field_diffs=(),
        blocked_reason=None,
        summary={
            "error_stage": where,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "not_applicable": dict(NOT_APPLICABLE_FIELDS),
            "by_construction_fields": list(BY_CONSTRUCTION_FIELDS),
            "scope": SCOPE_TEXT,
        },
    )


def run_differential(
    market: MarketArrays,
    candidates: CandidateArrays,
    cost: CostScenario,
    sizing: SizingSpec = DEFAULT_SIZING,
    rules: SimRules = DEFAULT_RULES,
    window: SimWindow | None = None,
    *,
    scenario_id: str,
    tolerances: dict[str, Tolerance] = DEFAULT_TOLERANCES,
    catalog_dir: Path | str | None = None,
    instrument: Any = None,
    _replay_mutator: Any = None,  # TEST HOOK: alters replay candidates only (non-vacuity tests)
) -> DifferentialResult:
    """Run FAST and the Nautilus candidate replay on the same bars/candidates and classify."""
    win = window if window is not None else SimWindow()
    if len(np.unique(candidates.decision_idx)) != len(candidates.decision_idx):
        return _blocked(
            scenario_id,
            0,
            "duplicate candidate decision indices: the replay identifies candidates by decision "
            "timestamp, so duplicates cannot be replayed faithfully",
        )
    trades = simulate_fast(market, candidates, cost, sizing, rules, window)
    n_fast = len(trades)

    # --- capability checks: explicit BLOCKED, never a silent PASS ------------------------------
    if cost.commission_eur_per_lot != 0.0 or cost.target_penetration_pts != 0.0:
        return _blocked(
            scenario_id,
            n_fast,
            "cost scenario not expressible in Nautilus: commission_eur_per_lot / "
            "target_penetration_pts must be 0 (instrument fees are 0, limit fills at the touch)",
        )
    if any(int(k) != int(EXIT_FIXED_R) for k in candidates.exit_kind):
        return _blocked(
            scenario_id,
            n_fast,
            "EXIT_TRAIL candidates: the replay strategy has no trailing-stop logic "
            "(the stop never moves)",
        )
    if any(int(r) == int(REASON_ENTRY_GAP_STOP) for r in trades.exit_reason):
        return _blocked(
            scenario_id,
            n_fast,
            "ENTRY_GAP_STOP: FAST fills at the gapped open beyond the stop; Nautilus bar book can "
            "only fill at the decision close, which is not a gap-entry",
        )
    try:
        open_ts = bar_open_ts_ns(market)
        fast_norm = normalize_fast(market, candidates, trades)
    except Exception as exc:  # malformed market (non-increasing day/minute) = input defect
        return _error(scenario_id, n_fast, "timestamps/normalize_fast", exc)

    netting = netting_report(trades, candidates)
    try:
        from nautilus_kernel.replay_backtest import ReplayCandidate, run_candidate_replay_backtest
    except ImportError as exc:  # pragma: no cover - environment without nautilus_trader
        return _blocked(scenario_id, n_fast, f"nautilus_trader unavailable: {exc!r}")

    try:
        inst = instrument if instrument is not None else _default_instrument()
    except (FileNotFoundError, ImportError) as exc:
        return _blocked(scenario_id, n_fast, f"instrument unavailable: {exc!r}")
    except Exception as exc:
        return _error(scenario_id, n_fast, "instrument", exc)

    try:
        tick = float(inst.price_increment)
        replay_cands = [
            ReplayCandidate(
                decision_ts_ns=t.signal_ts_ns,
                side=t.direction,
                stop=t.stop,
                target=t.target,
                qty=float(trades.qty[n]),
            )
            for n, t in enumerate(fast_norm)
        ]
        if _replay_mutator is not None:
            replay_cands = _replay_mutator(replay_cands)
        forced: dict[int, str] = {}
        for n in range(n_fast):
            kb, why = forced_exit_for(market, win, int(trades.entry_idx[n]))
            forced[int(open_ts[kb]) + BAR_NS] = why
        tmp: tempfile.TemporaryDirectory[str] | None = None
        if catalog_dir is None:
            tmp = tempfile.TemporaryDirectory(prefix="wb_diff_")
            cat = Path(tmp.name)
        else:
            cat = Path(catalog_dir)
            cat.mkdir(parents=True, exist_ok=True)
        try:
            _write_synthetic_catalog(cat, inst, market, cost.spread_mult, open_ts)
            replay = run_candidate_replay_backtest(
                catalog_path=cat,
                instrument=inst,
                timeframe=TIMEFRAME,
                candidates=replay_cands,
                forced_exits=forced,
                slippage_one_tick=cost.slippage_pts > 0,
            )
        finally:
            if tmp is not None:
                tmp.cleanup()
        fid_norm = normalize_replay(replay, market, cost, tick, open_ts)
    except Exception as exc:
        return _error(scenario_id, n_fast, "nautilus replay/normalize_replay", exc)

    try:
        return compare_trade_lists(
            scenario_id,
            fast_norm,
            fid_norm,
            market=market,
            cost=cost,
            tick=tick,
            open_ts=open_ts,
            trades=trades,
            tolerances=tolerances,
            contract_size=sizing.contract_size,
            extra_summary={
                "labels": list(replay.labels),
                "fast_skips": trades.skips,
                "nautilus_ignored_candidates": list(replay.ignored),
                "netting": {
                    **netting,
                    "replay_ignored_active_position": len(replay.ignored),
                },
            },
            extra_diffs=_netting_diffs(netting),
        )
    except Exception as exc:
        return _error(scenario_id, n_fast, "compare_trade_lists", exc)


def compare_trade_lists(
    scenario_id: str,
    fast_norm: list[NormalizedTrade],
    fid_norm: list[NormalizedTrade],
    *,
    market: MarketArrays | None = None,
    cost: CostScenario | None = None,
    tick: float = 0.01,
    open_ts: np.ndarray | None = None,
    trades: Any = None,
    contexts: dict[int, PairContext] | None = None,
    tolerances: dict[str, Tolerance] = DEFAULT_TOLERANCES,
    extra_summary: dict[str, Any] | None = None,
    contract_size: float = 1.0,
    extra_diffs: list[FieldDiff] | None = None,
) -> DifferentialResult:
    """Match trades by signal timestamp, classify all fields, derive status + summary.

    ``contexts`` (trade_index -> PairContext) may be given directly (used by unit tests);
    otherwise it is built from ``market``/``cost``/``trades`` (real engine run).
    """
    diffs: list[FieldDiff] = []
    fid_by_sig = {t.signal_ts_ns: t for t in fid_norm}
    fast_by_sig = {t.signal_ts_ns: t for t in fast_norm}
    cnt_cls = DiffClass.EXACT_MATCH if len(fast_norm) == len(fid_norm) else DiffClass.BUG_SUSPECTED
    diffs.append(
        FieldDiff(
            -1,
            "trade_count",
            len(fast_norm),
            len(fid_norm),
            float(len(fid_norm) - len(fast_norm)),
            cnt_cls,
            "" if cnt_cls is DiffClass.EXACT_MATCH else "trade counts differ",
            "TRADE",
        )
    )
    diffs.extend(extra_diffs or [])
    engine_timing: list[dict[str, Any]] = []
    per_trade_leg: list[str] = []
    per_trade_leg_strict: list[str] = []
    for n, f in enumerate(fast_norm):
        x = fid_by_sig.get(f.signal_ts_ns)
        if x is None:
            diffs.append(
                FieldDiff(
                    n,
                    "trade_presence",
                    "present",
                    "missing",
                    None,
                    DiffClass.BUG_SUSPECTED,
                    "FAST trade has no Nautilus counterpart (same signal timestamp)",
                    "TRADE",
                )
            )
            per_trade_leg.append("BOTH")
            per_trade_leg_strict.append("BOTH")
            continue
        if contexts is not None:
            ctx = contexts.get(n, PairContext(side=f.direction))
        else:
            assert market is not None and cost is not None and open_ts is not None
            assert trades is not None
            ctx = _build_context(
                f,
                x,
                market,
                cost,
                tick,
                open_ts,
                float(trades.qty[n]),
                int(trades.exit_idx[n]),
                int(trades.entry_idx[n]),
                int(trades.decision_idx[n]),
            )
            ctx = dataclasses.replace(ctx, contract_size=contract_size)
        pair = classify_pair(n, f, x, ctx, tolerances)
        diffs.extend(pair)
        per_trade_leg.append(trade_mismatch_leg(pair))
        engine_timing.append(
            {
                "signal_ts_ns": f.signal_ts_ns,
                "modeled_exit_ts_ns": x.exit_ts_ns,
                "engine_entry_ts_ns": x.engine_entry_ts_ns,
                "engine_exit_ts_ns": x.engine_exit_ts_ns,
            }
        )
        per_trade_leg_strict.append(trade_mismatch_leg(pair, strict=True))
    for n, x in enumerate(fid_norm):
        if x.signal_ts_ns not in fast_by_sig:
            diffs.append(
                FieldDiff(
                    n,
                    "trade_presence",
                    "missing",
                    "present",
                    None,
                    DiffClass.BUG_SUSPECTED,
                    "Nautilus trade has no FAST counterpart (same signal timestamp)",
                    "TRADE",
                )
            )

    # matched-field statistics only count INDEPENDENT checks (by-construction echoes excluded)
    class_counts = {c.value: 0 for c in DiffClass}
    by_construction_counts = {c.value: 0 for c in DiffClass}
    for d in diffs:
        (by_construction_counts if d.by_construction else class_counts)[d.diff_class.value] += 1
    matched = class_counts["EXACT_MATCH"] + class_counts["TOLERANCE_MATCH"]

    def _legs(vals: list[str]) -> dict[str, int]:
        return {k: vals.count(k) for k in ("ENTRY_MISMATCH", "EXIT_MISMATCH", "BOTH", "NONE")}

    unknown = [d for d in diffs if d.diff_class is DiffClass.BUG_SUSPECTED]
    status = "FAIL" if unknown else "PASS"
    summary: dict[str, Any] = {
        "class_counts": class_counts,
        "matched_fields": matched,
        "by_construction_class_counts": by_construction_counts,
        "by_construction_fields": list(BY_CONSTRUCTION_FIELDS),
        "scope": SCOPE_TEXT,
        "mismatch_leg_counts": _legs(per_trade_leg),
        "mismatch_leg_counts_strict": _legs(per_trade_leg_strict),
        "per_trade_mismatch_leg": per_trade_leg,
        "engine_timing": engine_timing,
        "not_applicable": dict(NOT_APPLICABLE_FIELDS),
        "bug_suspected_fields": sorted({d.field for d in unknown}),
        "tolerances": {k: {"abs_tol": v.abs_tol, "scale": v.scale} for k, v in tolerances.items()},
        **(extra_summary or {}),
    }
    return DifferentialResult(
        scenario_id=scenario_id,
        status=status,
        fast_trade_count=len(fast_norm),
        fidelity_trade_count=len(fid_norm),
        field_diffs=tuple(diffs),
        blocked_reason=None,
        summary=summary,
    )
