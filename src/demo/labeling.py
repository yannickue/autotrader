# ruff: noqa: E501
"""Post-hoc labelling: counterfactual labels for REJECTED opportunities and realised-trade outcomes.

Causality contract
  * Counterfactual labels use ONLY bars whose open time is >= the snapshot's `signal_ts_utc` (the
    close of the deciding bar) and < the horizon end. Bars before the signal are discarded even if a
    provider returns them. The snapshot/decision are never modified.
  * A label is only written once the horizon has elapsed (`now_utc >= horizon_end`) and the provider
    bars cover it (or a grace period passed, then the available bars are used).
  * R is ALWAYS measured against the initial risk distance: for counterfactuals `|intended_entry -
    stop|` (nothing was filled); for real trades `|actual fill - initial stop|`, never the intended entry.

Bar semantics mirror `alpha.fast.sim` (bid OHLC bars; a short's exit prices are ask = bid + spread):
  * inside one bar the STOP is checked before the target (pessimistic, stop-first);
  * a bar that opens through the stop exits at its open (gap);
  * MAE is updated on every bar including the stop bar, MFE is NOT updated on the bar that stops out;
  * fixed-R target is hit when the favourable extreme reaches it.
Hypothetical labels assume a fill AT `intended_entry` (no fill model, no slippage, no fees), so they
are an optimistic-for-fill, cost-free view of what the rejected setup would have done.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from demo.contracts import CounterfactualLabel, OpportunitySnapshot, OutcomeRecord
from demo.store import DemoStore, parse_utc

OPERATIONAL = "OPERATIONAL"  # gate class of cancelled / send-failed intents (halted, expired, restart, ...)
CATCHUP = "CATCHUP"  # gate class of CATCHUP_MISSED / EXPIRED_ENTRY / ALREADY_MOVED
SRC_ENGINE = "ENGINE_REJECTED"
SRC_CATCHUP = "CATCHUP_MISSED"
SRC_STACK = "STACK_REJECTED"
SRC_CANCELLED = "INTENT_CANCELLED"
SRC_SEND_FAILED = "SEND_FAILED"
SRC_SHADOW = "SHADOW_DRY_RUN"  # would-have-traded in shadow: labelled, but kept distinct from real non-trades
SRC_NO_INTENT = "ACCEPTED_NO_INTENT"
ACCEPTED_NO_INTENT_MIN_AGE_S = 600.0  # an accepted decision without an intent this old will never get one


@dataclass(frozen=True, slots=True)
class Bar:
    """Bid OHLC bar. `ts_utc` is the bar OPEN time (ISO UTC). `spread` in price units (ask = bid+spread)."""

    ts_utc: str
    open: float
    high: float
    low: float
    close: float
    spread: float = 0.0


BarsProvider = Callable[[str, str, str], Sequence[Bar]]
"""(market, start_utc, end_utc) -> bars with start <= open time < end. Filtered again here."""


@dataclass(frozen=True, slots=True)
class HypotheticalResult:
    r: float
    mfe_r: float
    mae_r: float
    target_before_stop: bool | None
    exit_kind: str  # STOP | STOP_GAP | TARGET | HORIZON
    bars_used: int


def simulate_hypothetical(
    *,
    direction: int,
    entry: float,
    stop: float,
    target: float | None,
    bars: Sequence[Bar],
    trail_r: float | None = None,
) -> HypotheticalResult:
    """Walk `bars` (already causal, time-ordered) from a hypothetical fill at `entry`."""
    risk = abs(entry - stop)
    if not risk > 0 or not math.isfinite(risk):
        raise ValueError("initial risk distance must be > 0")
    if direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
        raise ValueError("stop must be on the adverse side of entry")
    long = direction > 0
    trail = None if trail_r is None else trail_r * risk
    cur_stop = stop
    best = entry
    mfe = mae = 0.0
    last = entry
    used = 0
    for b in bars:
        used += 1
        sp = 0.0 if long else b.spread
        # adverse side: long exits at bid low; short exits at ask = bid + spread
        if long:
            gap = b.open <= cur_stop
            hit = b.low <= cur_stop
            adv = entry - b.low
            fav = b.high - entry
        else:
            gap = b.open + sp >= cur_stop
            hit = b.high + sp >= cur_stop
            adv = b.high + sp - entry
            fav = entry - (b.low + sp)
        mae = max(mae, adv / risk)
        if gap or hit:
            px = (b.open if long else b.open + sp) if gap else cur_stop
            r = ((px - entry) if long else (entry - px)) / risk
            return HypotheticalResult(
                r=r,
                mfe_r=mfe,
                mae_r=mae,
                target_before_stop=False,
                exit_kind="STOP_GAP" if gap else "STOP",
                bars_used=used,
            )
        if target is not None:
            reached = b.high >= target if long else (b.low + sp) <= target
            if reached:
                r = abs(target - entry) / risk
                return HypotheticalResult(
                    r=r,
                    mfe_r=max(mfe, r),
                    mae_r=mae,
                    target_before_stop=True,
                    exit_kind="TARGET",
                    bars_used=used,
                )
        elif trail is not None:
            if long:
                best = max(best, b.high)
                cur_stop = max(cur_stop, best - trail)
            else:
                best = min(best, b.low + sp)
                cur_stop = min(cur_stop, best + trail)
        mfe = max(mfe, fav / risk)
        last = b.close if long else b.close + sp
    r = ((last - entry) if long else (entry - last)) / risk
    # neither stop nor target reached by the horizon: target_before_stop is undecided (None)
    return HypotheticalResult(
        r=r, mfe_r=mfe, mae_r=mae, target_before_stop=None, exit_kind="HORIZON", bars_used=used
    )


def horizon_end_utc(snap: OpportunitySnapshot) -> datetime:
    return parse_utc(snap.signal_ts_utc) + timedelta(seconds=snap.geometry.expected_horizon_s)


def label_one(
    snap: OpportunitySnapshot,
    bars: Iterable[Bar],
    *,
    labelled_utc: str,
    horizon_end: datetime | None = None,
) -> tuple[CounterfactualLabel, HypotheticalResult]:
    """Pure: label from snapshot geometry + bars. Only bars in [signal_ts, horizon_end) are used."""
    start = parse_utc(snap.signal_ts_utc)
    end = horizon_end or horizon_end_utc(snap)
    causal = sorted(
        (b for b in bars if start <= parse_utc(b.ts_utc) < end), key=lambda b: parse_utc(b.ts_utc)
    )
    g = snap.geometry
    trail_r = g.exit_r if (g.target is None and g.exit_kind == "trail") else None
    res = simulate_hypothetical(
        direction=snap.direction,
        entry=g.intended_entry,
        stop=g.stop,
        target=g.target,
        bars=causal,
        trail_r=trail_r,
    )
    label = CounterfactualLabel(
        opportunity_id=snap.opportunity_id,
        phase=snap.phase,
        horizon_end_utc=end.isoformat(),
        hypothetical_mfe_r=res.mfe_r,
        hypothetical_mae_r=res.mae_r,
        hypothetical_r=res.r,
        target_before_stop=res.target_before_stop,
        labelled_utc=labelled_utc,
    )
    return label, res


@dataclass(frozen=True, slots=True)
class BlockingGate:
    """Why an opportunity was not traded: source + the gate that blocked it (primary + all codes)."""

    source: str
    code: str | None
    gate_class: str | None
    codes: tuple[str, ...]


def blocking_gate(row: dict) -> BlockingGate | None:
    """Attribution of one ``DemoStore.non_traded_unlabelled`` row; None = not (yet) a non-trade."""
    from demo.execution import gates as G
    from demo.opportunity.engine import CATCHUP_CODES
    from demo.opportunity.policy import GATE_CLASSIFICATION

    dec = row["decision"]
    state = row["intent_state"]
    detail = row.get("terminal_detail") or {}
    if not dec.accepted:
        codes = tuple(r for r in dec.reasons if r != "ACCEPTED")
        primary = codes[0] if codes else None
        if "CATCHUP_MISSED" in codes:
            return BlockingGate(SRC_CATCHUP, "CATCHUP_MISSED", CATCHUP, tuple(c for c in codes if c in CATCHUP_CODES))
        g = GATE_CLASSIFICATION.get(primary) if primary else None
        return BlockingGate(SRC_ENGINE, primary, g.gate_class if g else None, codes)
    if state == "RISK_REJECTED":
        raw = row.get("stack_code") or detail.get("reason") or "unknown"
        code = G.base_code(raw) if raw != "unknown" else raw
        gate = G.gate_for(raw) if raw != "unknown" else None
        cls = row.get("stack_class") or detail.get("gate_class") or (gate.gate_class.value if gate else None)
        return BlockingGate(SRC_STACK, code, cls, (code,))
    if state == "SEND_FAILED":
        code = str(detail.get("reason") or "SEND_FAILED")
        return BlockingGate(SRC_SEND_FAILED, code, detail.get("gate_class") or OPERATIONAL, (code,))
    if state == "CANCELLED":
        reason = str(detail.get("reason") or detail.get("restart") or "cancelled")
        if reason == "shadow_dry_run":
            return BlockingGate(SRC_SHADOW, "SHADOW_DRY_RUN", "SHADOW", ("SHADOW_DRY_RUN",))
        code = f"CANCELLED:{reason}"
        return BlockingGate(SRC_CANCELLED, code, OPERATIONAL, (code,))
    if state is None:
        return BlockingGate(SRC_NO_INTENT, "ACCEPTED_NO_INTENT", OPERATIONAL, ("ACCEPTED_NO_INTENT",))
    return None


def label_counterfactuals(
    store: DemoStore,
    bars_provider: BarsProvider,
    now_utc: str,
    *,
    phase: str | None = None,
    bar_seconds: int = 300,
    incomplete_grace_s: int = 6 * 3600,
) -> list[CounterfactualLabel]:
    """Label every NON-TRADED opportunity whose horizon elapsed: engine rejects, catch-up misses AND
    engine-accepted ones the stack rejected / cancelled / failed to send (incl. shadow dry-run, kept
    distinguishable through ``counterfactual_meta.source``).  Returns the labels newly written.

    Fill assumption (documented, optimistic): a fill AT the intended entry, no slippage / fees, no
    latency.  The blocking gate code + class are stored with each label for the per-gate funnel.

    Skipped (retried on a later call): horizon not elapsed; no bars; trade unresolved and bars do not
    yet cover the horizon and `incomplete_grace_s` has not passed since the horizon end (after the grace period the
    available bars are used, e.g. horizon runs past the session end).
    """
    now = parse_utc(now_utc)
    written: list[CounterfactualLabel] = []
    for row in store.non_traded_unlabelled(phase):
        snap = row["snapshot"]
        gate = blocking_gate(row)
        if gate is None:
            continue
        if (
            gate.source == SRC_NO_INTENT
            and (now - parse_utc(row["decision"].decided_utc)).total_seconds() < ACCEPTED_NO_INTENT_MIN_AGE_S
        ):
            continue  # the intent may still be created in this very cycle
        end = horizon_end_utc(snap)
        if now < end:
            continue
        raw = bars_provider(snap.market, snap.signal_ts_utc, end.isoformat())
        causal = [b for b in raw if parse_utc(snap.signal_ts_utc) <= parse_utc(b.ts_utc) < end]
        if not causal:
            continue
        last_open = max(parse_utc(b.ts_utc) for b in causal)
        covered = last_open + timedelta(seconds=bar_seconds) >= end
        label, res = label_one(snap, causal, labelled_utc=now.isoformat(), horizon_end=end)
        # A stop/target resolution inside the available bars is final; only a "still open at the last
        # bar" (HORIZON) result depends on coverage of the whole horizon.
        if (
            res.exit_kind == "HORIZON"
            and not covered
            and now < end + timedelta(seconds=incomplete_grace_s)
        ):
            continue
        if store.record_counterfactual(
            label,
            source=gate.source,
            gate_code=gate.code, gate_class=gate.gate_class, gate_codes=gate.codes,
        ):
            written.append(label)
    return written


# ---- realised outcome -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Fill:
    price: float
    quantity: float
    ts_utc: str


@dataclass(frozen=True, slots=True)
class PathPoint:
    """A bar (high/low) or a tick (high == low == price) between entry and exit, in the price basis the
    position exits at (bid for longs, ask for shorts)."""

    ts_utc: str
    high: float
    low: float


def outcome_from_fills(
    *,
    direction: int,
    entry_fill: Fill,
    initial_stop: float,
    exit_fills: Sequence[Fill],
    exit_reason: str,
    value_per_unit_eur: float,
    fees_eur: float = 0.0,
    swap_eur: float = 0.0,
    path: Iterable[PathPoint] = (),
) -> OutcomeRecord:
    """Derive the OutcomeRecord from ACTUAL fills.

    * initial risk distance = |entry_fill.price - initial_stop| (actual fill, never the intended entry).
    * `value_per_unit_eur`: EUR value of a 1.0 price move for quantity 1.0 (contract size x FX).
    * `fees_eur` / `swap_eur`: signed broker amounts (MT5 convention, negative = cost).
    * gross_r from price move only; net_r = gross_r + (fees + swap) / initial risk in EUR.
    * MFE/MAE >= 0 in R, from `path` points inside [entry, last exit] plus the fills themselves.
    * time_to_mfe/mae: seconds from entry to the first point reaching the extreme.
    """
    if direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if not exit_fills:
        raise ValueError("at least one exit fill required")
    risk = abs(entry_fill.price - initial_stop)
    if not risk > 0:
        raise ValueError("initial risk distance must be > 0")
    if (direction > 0 and initial_stop >= entry_fill.price) or (
        direction < 0 and initial_stop <= entry_fill.price
    ):
        raise ValueError("initial stop must be on the adverse side of the actual fill")
    qty_exit = sum(f.quantity for f in exit_fills)
    if qty_exit <= 0:
        raise ValueError("exit quantity must be > 0")
    exit_px = sum(f.price * f.quantity for f in exit_fills) / qty_exit
    entry_t = parse_utc(entry_fill.ts_utc)
    exit_t = max(parse_utc(f.ts_utc) for f in exit_fills)
    move = (exit_px - entry_fill.price) * direction
    gross_r = move / risk
    risk_eur = risk * qty_exit * value_per_unit_eur
    pnl_eur = move * qty_exit * value_per_unit_eur + fees_eur + swap_eur
    net_r = gross_r + (fees_eur + swap_eur) / risk_eur

    pts = [PathPoint(f.ts_utc, f.price, f.price) for f in (entry_fill, *exit_fills)]
    pts += [p for p in path if entry_t <= parse_utc(p.ts_utc) <= exit_t]
    pts.sort(key=lambda p: parse_utc(p.ts_utc))
    mfe = mae = 0.0
    t_mfe = t_mae = 0.0
    for p in pts:
        t = (parse_utc(p.ts_utc) - entry_t).total_seconds()
        fav = (
            (p.high if direction > 0 else -p.low)
            - (entry_fill.price if direction > 0 else -entry_fill.price)
        ) / risk
        adv = (
            (entry_fill.price if direction > 0 else -entry_fill.price)
            - (p.low if direction > 0 else -p.high)
        ) / risk
        if fav > mfe:
            mfe, t_mfe = fav, t
        if adv > mae:
            mae, t_mae = adv, t
    return OutcomeRecord(
        gross_r=gross_r,
        net_r=net_r,
        pnl_eur=pnl_eur,
        mfe_r=mfe,
        mae_r=mae,
        time_to_mfe_s=t_mfe if mfe > 0 else None,
        time_to_mae_s=t_mae if mae > 0 else None,
        holding_s=(exit_t - entry_t).total_seconds(),
        exit_reason=exit_reason,
        partial_fills=max(0, len(exit_fills) - 1),
        closed_utc=exit_t.isoformat(),
    )
