# ruff: noqa: E501
"""Lane X: thin offline harness that steps the EXISTING ``exits.ExitEngine`` (+ ``demo.structure`` E2 management
inputs) over the post-entry bars of ONE entry, under nine predeclared, strictly causal exit policies.

Purpose: compare exit policies on the SAME entry set (hypotheses only; forward evidence decides promotion).
Nothing here is searched or tuned: every parameter below is a small, VERSIONED constant (``POLICY_SET_VERSION``);
there is no grid over TP1/TP2/BE/trail/time thresholds and nothing is optimised until R turns positive.

The nine policies (all share the SAME entry fill and the SAME initial stop; the flat deadline ends every policy)

  P1 FIXED_1_5R          stop + one 1.5R target (the production default ``fixed_1_5r``); the baseline
  P2 STRUCT_TP1          stop + structural TP1 (100 %)                               [needs a structural TP1]
  P3 STRUCT_TP1_TP2      stop + structural TP1 (50 %) + structural TP2 (50 %)        [needs TP1 and TP2]
  P4 TP1_TP2_RUNNER      the PRODUCTION ``staged-e2-v1`` ExitPolicy (break-even after TP1, structure trailing,
                         structure failure, momentum / giveback / time-alpha / late-loser guards) with TP1 50 %,
                         TP2 25 %, runner 25 %; without a justified TP2: TP1 50 % + runner [needs a structural TP1]
  P5 STRUCT_TRAIL        stop only + trailing behind the newest CONFIRMED post-entry swing (no target)
  P6 BREAKEVEN_LOCK      P1 + cost-adjusted break-even once +1.0R was reached (the profit lock)
  P7 MOMENTUM_EXIT       P1 + the production momentum-deterioration exit (momentum <= -1.0 ATR over 3 bars)
  P8 TIME_ALPHA          P1 + time stop after 24 bars (2 h) unless the trade showed >= 0.5R (alpha decay)
  P9 EOD_FORCED_FLAT     initial stop only; the position is held to the Berlin flatten (Lane P operating policy)

Lane W (``eeq-policies-2``) adds three policies for the SHADOW EXIT LAB (``demo.shadow_exit_lab``); ``POLICY_IDS`` stays the
nine above (Lane X results reproduce bit-for-bit), ``LAB_POLICY_IDS`` is the twelve:

  P10 TP1_PLUS_RUNNER    the PRODUCTION staged policy with ONLY a structural TP1 (50 %) + runner; never a TP2 [needs a structural TP1]
  P11 BE_PLUS_RUNNER     no target; once +1.0R was reached the stop moves to the engine's COST-ADJUSTED break-even floor (from the
                         next bar) and the remainder trails behind confirmed structure.  The floor is the engine's own ratchet
                         (never loosens, never above the price); the LIVE production break-even is NOT an independent authority:
                         it exists only after TP1 inside the staged policy (``breakeven_after_first_stage``)
  P12 FAILED_MOVE_EXIT   P1 + exit of the remainder at the CLOSE of the first bar that closes back across the failed-move level
                         (the broken range edge) against the trade: the reclaim failed / the break re-opened.  [needs the level]

``[needs ...]``: an entry without the required structural level is NOT_APPLICABLE for that policy (never a level
invented from an R multiple).  Comparisons are therefore also reported on the paired subset on which every
structural policy applies.

Bar-resolution stepping (conservative, stop-first)
  Each bar is fed to ``ExitEngine.evaluate`` as four ticks in EXIT-SIDE prices (long: bid; short: ask = bid + spread):
  T0 open, T1 adverse extreme (stop checks), T2 favourable extreme (target fills, break-even / trail ratchets),
  T3 close (momentum / structure / time / giveback rules and the swing-based trail candidate from CLOSED bars).
  * The stop of a bar is the stop in force at the bar's start: a ratchet triggered inside a bar applies from the NEXT
    bar (never same-bar benefit).
  * A stop hit fills at the stop price (at the open price when the bar opened through it = gap); a target fills at the
    target level (no price improvement); a non-stop, non-target rule exit fills at the open (T0) or the close (T3).
  * The forced flat closes the remainder at the OPEN of the first bar at or after the flat instant
    (``OperatingPolicy.effective_flat_utc``: Berlin flatten start 21:55, per-market session close buffer).
  * R = spread-adjusted gross R against the initial risk |fill - stop| (entry already pays the ask for a long); no
    commission / slippage.  Partial fills are weighted by their fraction of the original position.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from demo import structure as st
from exits.engine import ExitEngine, apply_evaluation
from exits.models import (
    ExitMarketState,
    ExitPolicy,
    ExitPosition,
    ExitReason,
    PositionSide,
    TakeProfitStage,
)

POLICY_SET_VERSION = "eeq-policies-2"  # 2: Lane W adds P10-P12 (P1-P9 unchanged); parameters unchanged

P_FIXED = "P1_FIXED_1_5R"
P_TP1 = "P2_STRUCT_TP1"
P_TP12 = "P3_STRUCT_TP1_TP2"
P_RUNNER = "P4_TP1_TP2_RUNNER"
P_TRAIL = "P5_STRUCT_TRAIL"
P_BE = "P6_BREAKEVEN_LOCK"
P_MOM = "P7_MOMENTUM_EXIT"
P_TIME = "P8_TIME_ALPHA"
P_EOD = "P9_EOD_FORCED_FLAT"
P_TP1_RUNNER = "P10_TP1_PLUS_RUNNER"
P_BE_RUNNER = "P11_BE_PLUS_RUNNER"
P_FM = "P12_FAILED_MOVE_EXIT"
POLICY_IDS: tuple[str, ...] = (P_FIXED, P_TP1, P_TP12, P_RUNNER, P_TRAIL, P_BE, P_MOM, P_TIME, P_EOD)
LAB_POLICY_IDS: tuple[str, ...] = (*POLICY_IDS, P_TP1_RUNNER, P_BE_RUNNER, P_FM)
STRUCTURAL_POLICIES = (P_TP1, P_TP12, P_RUNNER)

# ---- predeclared, versioned parameters (NOT searched) ---------------------------------------------------
FIXED_R = Decimal("1.5")
BE_TRIGGER_R = Decimal("1.0")
MOMENTUM_THRESHOLD = Decimal("-1.0")  # production value (demo.execution.exit_manager.default_staged_exit_policy)
TIME_STOP_BARS = 24  # 2 h of M5
TIME_STOP_MIN_MFE_R = Decimal("0.5")
TP12_FRACTIONS = (Decimal("0.5"), Decimal("0.5"))
TP1_ONLY_FRACTIONS = (Decimal("1"),)
RUNNER_FRACTIONS = (Decimal("0.5"), Decimal("0.25"))  # production DEFAULT_STAGE_FRACTIONS; runner = remainder
BAR_SECONDS = 300
POLICY_PARAMS: dict[str, Any] = {
    "version": POLICY_SET_VERSION,
    "fixed_r": str(FIXED_R), "be_trigger_r": str(BE_TRIGGER_R), "momentum_threshold_atr": str(MOMENTUM_THRESHOLD),
    "time_stop_bars": TIME_STOP_BARS, "time_stop_min_mfe_r": str(TIME_STOP_MIN_MFE_R),
    "tp12_fractions": [str(x) for x in TP12_FRACTIONS], "runner_fractions": [str(x) for x in RUNNER_FRACTIONS],
    "failed_move_rule": "close (bid) back across the failed-move level against the trade -> exit at that close; level must lie strictly between stop and fill",
    "be_runner": "break-even trigger 1.0R (engine cost-adjusted floor, from the next bar) + structure trailing, no target",
    "swing_n": 2, "atr_buffer_mult": 0.25, "momentum_bars": 3,
    "tick_model": "T0 open, T1 adverse extreme, T2 favourable extreme, T3 close; stop-first; ratchets apply from the next bar",
    "sizing": "normalised position 1.0; R = spread-adjusted gross against initial risk |fill-stop|",
}

EXIT_FORCED_FLAT = "FORCED_FLAT"
EXIT_DATA_END = "DATA_END"


class _NullGate:
    def release(self, decision_id: str) -> None:  # pragma: no cover - the harness never notifies terminals
        return None


def _d(x: float | int | str | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(repr(float(x)) if isinstance(x, float) else str(x))


@dataclass(frozen=True, slots=True)
class BarSeries:
    """Post-entry bars (BID OHLC, spread in price units), time-ordered; ``ts`` = bar OPEN (UTC-aware)."""

    ts: tuple[datetime, ...]
    o: tuple[float, ...]
    h: tuple[float, ...]
    lo: tuple[float, ...]
    c: tuple[float, ...]
    spread: tuple[float, ...]

    def __len__(self) -> int:
        return len(self.ts)


@dataclass(frozen=True, slots=True)
class EntryInput:
    entry_id: str
    market: str
    direction: int
    fill: float  # long: ask (open + spread); short: bid (open)
    stop: float
    entry_ts: datetime  # OPEN of the entry bar (= the signal bar close)
    atr: float | None
    tp1: float | None = None
    tp2: float | None = None
    tp1_id: str = "tp1"
    tp2_id: str = "tp2"
    flat_utc: datetime | None = None
    pre: BarSeries | None = None  # the last closed bars BEFORE the entry (momentum / swing context, >= 5 bars)
    failed_move_level: float | None = None  # Lane W: broken range edge; a close back across it against the trade = failed move


@dataclass(slots=True)
class Fill:
    fraction: float
    price: float
    r: float
    reason: str
    ts: datetime


@dataclass(slots=True)
class PolicyResult:
    policy_id: str
    applicable: bool
    na_reason: str | None = None
    r: float | None = None
    fills: list[Fill] = field(default_factory=list)
    final_reason: str | None = None
    holding_s: float | None = None
    censored: bool = False
    stages_hit: int = 0
    stop_levels: list[float] = field(default_factory=list)  # the stop in force after every change (initial first)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "applicable": self.applicable, "na_reason": self.na_reason, "r": self.r,
            "final_reason": self.final_reason, "holding_s": self.holding_s, "censored": self.censored,
            "stages_hit": self.stages_hit,
            "fills": [{"fraction": f.fraction, "price": f.price, "r": f.r, "reason": f.reason, "ts": f.ts.isoformat()} for f in self.fills],
        }


def _base_policy(policy_id: str, **kw: Any) -> ExitPolicy:
    return ExitPolicy(
        policy_id=policy_id, breakeven_trigger_r_multiple=kw.pop("breakeven_trigger_r_multiple", Decimal("99")),
        trailing_activation_r_multiple=Decimal("99"), trailing_distance_volatility_multiplier=Decimal("2"),
        max_market_data_age=timedelta(seconds=30), **kw,
    )


def _policy_object(pid: str) -> ExitPolicy:
    if pid in (P_FIXED, P_TP1, P_TP12, P_EOD, P_FM):
        return _base_policy(f"eeq-{pid}", **({"structure_failure_exit": True} if pid == P_FM else {}))
    if pid == P_BE_RUNNER:
        return _base_policy(f"eeq-{pid}", breakeven_trigger_r_multiple=BE_TRIGGER_R, structure_trailing=True)
    if pid in (P_RUNNER, P_TP1_RUNNER):
        from demo.execution.exit_manager import default_staged_exit_policy

        return default_staged_exit_policy()  # the PRODUCTION staged policy, unmodified
    if pid == P_TRAIL:
        return _base_policy(f"eeq-{pid}", structure_trailing=True)
    if pid == P_BE:
        return _base_policy(f"eeq-{pid}", breakeven_trigger_r_multiple=BE_TRIGGER_R)
    if pid == P_MOM:
        return _base_policy(f"eeq-{pid}", momentum_deterioration_threshold=MOMENTUM_THRESHOLD)
    if pid == P_TIME:
        return _base_policy(f"eeq-{pid}", max_holding_duration=timedelta(seconds=TIME_STOP_BARS * BAR_SECONDS), time_stop_min_mfe_r=TIME_STOP_MIN_MFE_R)
    raise ValueError(pid)


def _stages(pid: str, e: EntryInput) -> tuple[tuple[TakeProfitStage, ...], str | None]:
    """Per-position target ladder of a policy, or (``()``, reason) when its structural levels are missing."""
    if pid in (P_TRAIL, P_EOD, P_BE_RUNNER):
        return (), None
    if pid == P_FM:
        lvl, long = e.failed_move_level, e.direction == 1
        if lvl is None:
            return (), "NO_FAILED_MOVE_LEVEL"
        if not ((e.stop < lvl < e.fill) if long else (e.fill < lvl < e.stop)):
            return (), "FAILED_MOVE_LEVEL_NOT_BETWEEN_STOP_AND_ENTRY"
    if pid in (P_FIXED, P_BE, P_MOM, P_TIME, P_FM):
        return (TakeProfitStage(close_fraction=Decimal(1), r_multiple=FIXED_R, stage_id="fixed_r"),), None
    if e.tp1 is None:
        return (), "NO_STRUCTURAL_TP1"
    tp1 = TakeProfitStage(close_fraction=Decimal(1), target_price=_d(e.tp1), stage_id="tp1", source=f"STRUCTURE:{e.tp1_id}")
    if pid == P_TP1:
        return (tp1,), None
    if pid == P_TP12:
        if e.tp2 is None:
            return (), "NO_STRUCTURAL_TP2"
        return (
            replace(tp1, close_fraction=TP12_FRACTIONS[0]),
            TakeProfitStage(close_fraction=TP12_FRACTIONS[1], target_price=_d(e.tp2), stage_id="tp2", source=f"STRUCTURE:{e.tp2_id}"),
        ), None
    if pid == P_TP1_RUNNER:
        return (replace(tp1, close_fraction=RUNNER_FRACTIONS[0]),), None
    if pid == P_RUNNER:
        stages = [replace(tp1, close_fraction=RUNNER_FRACTIONS[0])]
        if e.tp2 is not None:
            stages.append(TakeProfitStage(close_fraction=RUNNER_FRACTIONS[1], target_price=_d(e.tp2), stage_id="tp2", source=f"STRUCTURE:{e.tp2_id}"))
        return tuple(stages), None
    raise ValueError(pid)


class ManagementCache:
    """The E2 management inputs of ONE entry, derived ONCE from pre + post-entry closed bars and shared by the
    policies: the M5 fractal swings of the whole window (causal: each is filtered to ``confirmed_at <= as_of`` by
    ``management_signals_from_swings``) and the close series."""

    def __init__(self, e: EntryInput, bars: BarSeries) -> None:
        pre = e.pre
        ts = list(pre.ts) + list(bars.ts) if pre else list(bars.ts)
        cols = {
            "ts": pd.DatetimeIndex(ts),
            "open": (list(pre.o) if pre else []) + list(bars.o),
            "high": (list(pre.h) if pre else []) + list(bars.h),
            "low": (list(pre.lo) if pre else []) + list(bars.lo),
            "close": (list(pre.c) if pre else []) + list(bars.c),
        }
        self.n_pre = len(pre) if pre else 0
        self.closes: list[float] = list(cols["close"])
        self.swings = st.confirmed_swings(pd.DataFrame(cols), 2, "M5") if len(ts) >= 5 else []

    def signals(self, e: EntryInput, k: int, *, current_stop: Decimal, price: Decimal, spread: Decimal, as_of: datetime) -> st.ManagementSignals:
        return st.management_signals_from_swings(
            e.direction, closes=self.closes[: self.n_pre + k + 1], as_of=as_of, swings=self.swings,
            entered_at=e.entry_ts, current_stop=current_stop, price=price, atr=None if e.atr is None else _d(e.atr),
            spread=spread,
        )


def simulate_policy(pid: str, e: EntryInput, bars: BarSeries, mgmt: ManagementCache | None = None) -> PolicyResult:
    """Run ONE policy on ONE entry over ``bars`` (the entry bar first).  Pure and deterministic."""
    if pid not in LAB_POLICY_IDS:
        raise ValueError(f"unknown policy {pid!r}")
    stages, na = _stages(pid, e)
    if na is not None:
        return PolicyResult(pid, False, na_reason=na)
    policy = _policy_object(pid)
    engine = ExitEngine(policy=policy, risk_gate=_NullGate())
    long = e.direction == 1
    side = PositionSide.LONG if long else PositionSide.SHORT
    entry_d, stop_d = _d(e.fill), _d(e.stop)
    risk = abs(entry_d - stop_d)
    if not risk > 0:
        return PolicyResult(pid, False, na_reason="ZERO_RISK")
    needs_signals = (
        policy.structure_trailing or policy.structure_failure_exit or policy.momentum_deterioration_threshold is not None
        or policy.late_loser_momentum_threshold is not None
    ) and pid != P_FM  # P12 replaces the swing-based structure failure by the explicit failed-move level rule
    if needs_signals and mgmt is None:
        mgmt = ManagementCache(e, bars)
    pos = ExitPosition(
        position_id=e.entry_id, instrument=e.market, side=side, entry_price=entry_d, quantity=Decimal(1),
        initial_stop_price=stop_d, current_stop_price=stop_d, opened_at=e.entry_ts, target_stages=stages,
    )
    res = PolicyResult(pid, True)
    res.stop_levels.append(float(stop_d))
    sgn = 1 if long else -1

    def r_of(px: Decimal) -> float:
        return float((px - entry_d) * sgn / risk)

    def close_fill(q: Decimal, px: Decimal, reason: str, ts: datetime) -> None:
        res.fills.append(Fill(float(q), float(px), r_of(px), reason, ts))

    def exit_side(b: float, sp: float) -> Decimal:
        return _d(b if long else b + sp)

    last_close_px: Decimal | None = None
    last_ts = e.entry_ts
    for k in range(len(bars)):
        t_open = bars.ts[k]
        sp = bars.spread[k]
        sp_d = _d(sp)
        o_x = exit_side(bars.o[k], sp)
        if e.flat_utc is not None and t_open >= e.flat_utc:
            close_fill(pos.quantity, o_x, EXIT_FORCED_FLAT, t_open)
            pos = replace(pos, quantity=Decimal(0))
            break
        adv_x = exit_side(bars.lo[k] if long else bars.h[k], sp)
        fav_x = exit_side(bars.h[k] if long else bars.lo[k], sp)
        c_x = exit_side(bars.c[k], sp)
        t_close = t_open + timedelta(seconds=BAR_SECONDS)
        ticks = (("T0", o_x, t_open), ("T1", adv_x, t_open), ("T2", fav_x, t_open), ("T3", c_x, t_close))
        done = False
        for name, px, now in ticks:
            for _ in range(6):  # several stages may be gapped through in one tick: one decision per evaluate
                hw = pos.effective_high_water_mark
                hw_seen = max(hw, px) if long else min(hw, px)
                mfe_r = ((hw_seen - entry_d) if long else (entry_d - hw_seen)) / risk
                cur_r = ((px - entry_d) if long else (entry_d - px)) / risk
                sig = None
                if name == "T3" and needs_signals and mgmt is not None:
                    sig = mgmt.signals(e, k, current_stop=pos.current_stop_price, price=px, spread=sp_d, as_of=t_close)
                left = None if e.flat_utc is None else max(timedelta(0), e.flat_utc - now)
                bid = px if long else px - sp_d
                ask = px + sp_d if long else px
                market = ExitMarketState(
                    instrument=e.market, timestamp=now, price=px, bid=bid, ask=ask,
                    mfe_r=max(mfe_r, cur_r, Decimal(0)),
                    giveback_r=max(Decimal(0), mfe_r - cur_r) if name in ("T0", "T3") else None,
                    holding_seconds=Decimal(str((now - e.entry_ts).total_seconds())),
                    expected_exit_cost=sp_d,
                    structure_trail_price=None if sig is None else sig.trail_candidate,
                    structure_failure=(
                        (bars.c[k] < e.failed_move_level if long else bars.c[k] > e.failed_move_level)  # type: ignore[operator]
                        if (pid == P_FM and name == "T3") else (False if sig is None else sig.structure_failure)
                    ),
                    momentum_score=None if sig is None else sig.momentum_score,
                    time_to_forced_flat=left,
                )
                ev = engine.evaluate(position=pos, market=market, now=now)
                pos = apply_evaluation(pos, ev)
                if float(pos.current_stop_price) != res.stop_levels[-1]:
                    res.stop_levels.append(float(pos.current_stop_price))
                d = ev.decision
                if d is None:
                    break
                reason = d.reason
                if reason in (ExitReason.INVALIDATION_STOP, ExitReason.TRAILING_STOP):
                    stop_px = Decimal(str(d.metadata["stop_price"]))
                    gap = (px < stop_px) if long else (px > stop_px)
                    fill_px = px if (name == "T0" and gap) else stop_px
                    label = "STOP_GAP" if (name == "T0" and gap) else ("TRAILING_STOP" if reason is ExitReason.TRAILING_STOP else ("BREAK_EVEN_STOP" if d.metadata.get("stop_stage") == "break_even" else "STOP"))
                elif reason is ExitReason.TAKE_PROFIT:
                    fill_px = Decimal(str(d.metadata["target_price"]))
                    label = f"TP{int(d.metadata.get('stage_index', 0)) + 1}"
                else:
                    if name not in ("T0", "T3"):  # pragma: no cover - defensive: the tick model forbids it
                        raise AssertionError(f"rule exit {reason} at {name}")
                    fill_px, label = px, reason.value
                close_fill(d.quantity, fill_px, label, now)
                if d.is_partial:
                    res.stages_hit += 1
                    pos = replace(
                        pos, quantity=pos.quantity - d.quantity,
                        realized_partial_quantity=pos.realized_partial_quantity + d.quantity,
                        stages_completed=pos.stages_completed + (1 if reason is ExitReason.TAKE_PROFIT else 0),
                    )
                    continue  # re-evaluate the same tick for the next stage
                if reason is ExitReason.TAKE_PROFIT:
                    res.stages_hit += 1
                pos = replace(pos, quantity=Decimal(0))
                done = True
                break
            if done or pos.quantity <= 0:
                done = True
                break
        last_close_px, last_ts = c_x, t_close
        if done:
            break
    else:
        if pos.quantity > 0 and last_close_px is not None:  # data ended before the flat deadline: censored
            close_fill(pos.quantity, last_close_px, EXIT_DATA_END, last_ts)
            res.censored = True
            pos = replace(pos, quantity=Decimal(0))
    if not res.fills:
        return PolicyResult(pid, False, na_reason="NO_BARS")
    res.r = sum(f.fraction * f.r for f in res.fills)
    res.final_reason = res.fills[-1].reason
    res.holding_s = max(0.0, (res.fills[-1].ts - e.entry_ts).total_seconds())
    return res


def simulate_all(e: EntryInput, bars: BarSeries, policy_ids: Sequence[str] = POLICY_IDS) -> dict[str, PolicyResult]:
    """All policies on the SAME entry and bars (the management inputs are derived once)."""
    mgmt = ManagementCache(e, bars)
    return {pid: simulate_policy(pid, e, bars, mgmt) for pid in policy_ids}
