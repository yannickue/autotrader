# ruff: noqa: E501
"""Rejection funnel of the DEMO trader: WHY did an opportunity not become a trade, by gate class.

Combines, from the persisted store (durable across restarts) and optionally the live stack:

* (a) ENGINE policy rejects per opportunity (``Decision.reasons``, classified by
  ``demo.opportunity.policy.GATE_CLASSIFICATION``);
* (b) STACK rejections (``RISK_REJECTED`` intents and refusals after sizing) classified by
  ``demo.execution.gates.GATE_CATALOG`` (SAFETY / STRUCTURAL / TEMPORARY / QUALITY / LEGACY_ARBITRARY),
  with ``otherwise_valid_blocked`` for TEMPORARY limitations (v1 add-on / opposite-side);
* (c) the same numbers per market and per family;
* (d) "trades that would have existed" counts: how many technically valid opportunities were lost
  to each class of gate, and what the trade count would have been without the TEMPORARY / LEGACY
  gates.

Pure read model: never writes, never influences trading. Quality inputs (confidence, confluence,
family score, ...) never appear as reject reasons - they are logged / ranked only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from demo.execution import gates as G
from demo.opportunity.arbitration import (
    ADD_ON_CANDIDATE,
    ARBITRATION_CODES,
    CONCURRENT_SIGNAL,
    REVERSAL_CANDIDATE,
    classify_stack_code,
)
from demo.opportunity.policy import (
    GATE_CLASSIFICATION,
    OUT_OF_WINDOW_SHADOW,
    SHADOW_SCAN_GATES,
    SHADOW_UNIVERSE,
)

# ---- sequential stages: an opportunity is stopped at the FIRST stage one of its gate codes belongs to ----
STAGES: tuple[str, ...] = (
    "RAW", "STRUCTURAL_VALID", "TRADABLE", "STRATEGY_WINDOW", "COST", "DUPLICATE", "PORTFOLIO", "RISK",
    "EXECUTABLE", "ACCEPTED", "EXECUTED",
)
_SI = {s: i for i, s in enumerate(STAGES)}
_ENGINE_STAGE: dict[str, str] = {
    CONCURRENT_SIGNAL: "PORTFOLIO", ADD_ON_CANDIDATE: "PORTFOLIO", REVERSAL_CANDIDATE: "PORTFOLIO",
    "NO_STRUCTURAL_STOP": "STRUCTURAL_VALID", "TARGET_ALREADY_CROSSED": "STRUCTURAL_VALID",
    "SPACE_BELOW_MIN_R": "STRUCTURAL_VALID",
    "CLOCK_ANOMALY": "TRADABLE", "MARKET_CLOSED": "TRADABLE", "STALE_SIGNAL": "TRADABLE",
    "ENTRY_OVERSHOT": "TRADABLE", "CATCHUP_MISSED": "TRADABLE", "EXPIRED_ENTRY": "TRADABLE",
    "ALREADY_MOVED": "TRADABLE",
    "OUTSIDE_ENTRY_WINDOW": "STRATEGY_WINDOW", OUT_OF_WINDOW_SHADOW: "STRATEGY_WINDOW",
    "SPREAD_TOO_WIDE": "COST",
    "DUPLICATE_OPPORTUNITY": "DUPLICATE",
}
_STACK_STAGE: dict[str, str] = {
    G.R_TARGET_CROSSED: "STRUCTURAL_VALID", G.R_INVALIDATION_CROSSED: "STRUCTURAL_VALID",
    G.R_MIN_SPACE_R: "STRUCTURAL_VALID", G.R_NO_STOP: "STRUCTURAL_VALID",
    G.R_ENTRY_OVERSHOOT: "TRADABLE", G.R_STALE_SIGNAL: "TRADABLE", G.R_SIGNAL_STALE: "TRADABLE",
    G.R_PAST_FORCED_FLAT: "TRADABLE", G.R_FLATTEN_WINDOW: "TRADABLE", G.R_ENTRY_RUNWAY: "TRADABLE",
    G.R_OUTSIDE_OPERATING_DAY: "TRADABLE", G.R_STALE_FEED: "TRADABLE", G.R_DATA_STALE: "TRADABLE",
    G.R_SPREAD_CAP: "COST", G.R_SPREAD_TOO_WIDE: "COST",
    G.R_DUPLICATE_INTENT: "DUPLICATE",
    G.R_ADDON: "PORTFOLIO", G.R_ADDON_SHARED: "PORTFOLIO", G.R_OPPOSITE: "PORTFOLIO",
    G.R_EXPOSURE_LIMIT: "PORTFOLIO", G.R_UNKNOWN_CLUSTER: "PORTFOLIO", G.R_FOREIGN_POSITION: "PORTFOLIO",
    "canary_position_open": "PORTFOLIO",
    G.R_SIZE_BELOW_MIN: "RISK", G.R_DAILY_LOSS: "RISK", G.R_DRAWDOWN: "RISK", G.R_CONSECUTIVE_LOSSES: "RISK",
    G.R_EQUITY: "RISK", G.R_INVALID_INPUT: "RISK", G.R_INVALID_STOP: "RISK", G.R_INVALID_MARKET_FACTS: "RISK",
    G.R_RISK_FRACTION_INVALID: "RISK", G.R_MARGIN_LIQUIDATION: "RISK", G.R_MARGIN_BEYOND: "RISK",
    G.R_RISK_ERROR: "RISK",
}
_PORTFOLIO_CAPS = frozenset({
    "max_aggregate_open_stop_risk_fraction", "max_cluster_stop_risk_fraction", "max_family_share_of_open_risk",
    "max_portfolio_leverage",
})
SPECIAL_BUCKETS: dict[str, tuple[str, ...]] = {
    "MISSED": ("CATCHUP_MISSED",),
    "EXPIRED": ("EXPIRED_ENTRY", "CANCELLED:expired", G.R_STALE_SIGNAL, G.R_SIGNAL_STALE),
    "ALREADY_MOVED": ("ALREADY_MOVED",),
    "ADDON_NOT_SUPPORTED": (G.R_ADDON, G.R_ADDON_SHARED),
    "OPPOSITE_NOT_SUPPORTED": (G.R_OPPOSITE,),
}
STRUCTURE_ATR_STEP = 0.25  # stops within the same 0.25-ATR zone (same day, market, family, direction) = one structure

CLASSES = ("SAFETY", "STRUCTURAL", "TEMPORARY", "QUALITY", "LEGACY_ARBITRARY")
NON_HARD_ENGINE = frozenset({"LEGACY_ARBITRARY", "QUALITY", "TEMPORARY"})
TRADED_STATES = frozenset({"FILLED", "PROTECTED", "CLOSED"})
UNCLASSIFIED = "UNCLASSIFIED"


def engine_class(code: str) -> str:
    g = GATE_CLASSIFICATION.get(code) or SHADOW_SCAN_GATES.get(code)
    if g is None and code in ARBITRATION_CODES:
        return "TEMPORARY"  # Lane S: symbol occupied (MT5 netting), same family of limitation as the stack's ADDON_*/OPPOSITE codes
    return g.gate_class if g is not None else UNCLASSIFIED


def stack_class(code: str | None, recorded: str | None = None) -> str:
    if recorded:
        return recorded
    if not code:
        return UNCLASSIFIED
    g = G.gate_for(code)
    return g.gate_class.value if g is not None else UNCLASSIFIED


def _base(code: str) -> str:
    return G.base_code(code)


class _Bucket:
    """Counters for one slice (overall / one market / one family)."""

    __slots__ = (
        "engine_accepted", "engine_by_class", "engine_only_non_hard", "engine_reasons", "engine_rejected",
        "opportunities", "shadow_would_trade", "stack_approved_not_traded", "stack_by_class", "stack_codes", "stack_rejected",
        "temporary_blocked", "temporary_codes", "traded",
    )

    def __init__(self) -> None:
        self.opportunities = self.engine_accepted = self.engine_rejected = 0
        self.engine_only_non_hard = self.stack_rejected = self.temporary_blocked = 0
        self.traded = self.stack_approved_not_traded = self.shadow_would_trade = 0
        self.engine_reasons: Counter[str] = Counter()
        self.engine_by_class: Counter[str] = Counter()
        self.stack_codes: Counter[str] = Counter()
        self.stack_by_class: Counter[str] = Counter()
        self.temporary_codes: Counter[str] = Counter()

    def add(self, row: Mapping[str, Any]) -> None:
        self.opportunities += 1
        if row["accepted"] is False:
            self.engine_rejected += 1
            reasons = [r for r in row["reasons"] if r != "ACCEPTED"]
            self.engine_reasons.update(reasons)
            classes = {engine_class(r) for r in reasons}
            self.engine_by_class.update(classes)
            if reasons and classes <= NON_HARD_ENGINE:
                self.engine_only_non_hard += 1
            return
        if row["accepted"] is None:
            return
        self.engine_accepted += 1
        state = row["state"]
        code = row["stack_reject_code"]
        rejected = state == "RISK_REJECTED" or bool(code)
        if row.get("shadow_dry_run") and not rejected:
            self.shadow_would_trade += 1  # shadow DRY-RUN approval: never a trade, kept out of trade metrics
        elif state in TRADED_STATES and not rejected:
            self.traded += 1
        elif rejected:
            cls = stack_class(code, row["stack_gate_class"])
            base = _base(code) if code else "unknown"
            self.stack_rejected += 1
            self.stack_codes[base] += 1
            self.stack_by_class[cls] += 1
            if cls == "TEMPORARY" and row["otherwise_valid"] is not False:
                self.temporary_blocked += 1
                self.temporary_codes[base] += 1
        elif row["approved"] is True:
            self.stack_approved_not_traded += 1

    def as_dict(self) -> dict[str, Any]:
        would_without_temp = self.traded + self.stack_approved_not_traded + self.shadow_would_trade + self.temporary_blocked
        legacy_stack = self.stack_by_class.get("LEGACY_ARBITRARY", 0)
        return {
            "opportunities": self.opportunities,
            "engine_accepted": self.engine_accepted,
            "engine_rejected": self.engine_rejected,
            "engine_reasons": dict(self.engine_reasons.most_common()),
            "engine_by_class": dict(self.engine_by_class),
            "engine_rejected_only_by_non_hard_classes": self.engine_only_non_hard,
            "stack_rejected": self.stack_rejected,
            "stack_codes": dict(self.stack_codes.most_common()),
            "stack_by_class": dict(self.stack_by_class),
            "traded": self.traded,
            "stack_approved_not_traded": self.stack_approved_not_traded,
            "shadow_would_trade": self.shadow_would_trade,
            "temporary_otherwise_valid_blocked": self.temporary_blocked,
            "trades_that_would_have_existed": {
                "actual": self.traded,
                "without_temporary_limitations": would_without_temp,
                "without_temporary_and_legacy_stack_gates": would_without_temp + legacy_stack,
                "engine_side_without_legacy_quality_temporary": self.engine_accepted + self.engine_only_non_hard,
            },
        }


def arbitration_counts(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Signals that met an occupied symbol (MT5 netting): engine-level same-cycle losers AND stack ADDON_*/OPPOSITE rejects,
    counted per explicit code. ``raw_signals`` are NOT independent (see ``structure_event_clusters``)."""
    out: Counter[str] = Counter()
    for r in rows:
        if r["accepted"] is False:
            codes = {c for c in r["reasons"] if c in ARBITRATION_CODES}
        elif r["accepted"] and (r["state"] == "RISK_REJECTED" or r["stack_reject_code"]):
            codes = set(classify_stack_code(r["stack_reject_code"]))
        else:
            continue
        out.update(codes)
    return {c: out.get(c, 0) for c in ARBITRATION_CODES}


def structure_event_clusters(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Event-level (clustered) counts next to the raw counts. Variants that react to ONE structure break share a
    ``structure_event_id`` and are highly related, NOT independent evidence: four variants firing on one break is ONE event.
    Rows without an id (non-STRUCT families / old data) are reported as ``unattributed`` and never merged."""
    events: dict[str, dict[str, Any]] = {}
    unattributed = 0
    for r in rows:
        eid = r.get("structure_event_id")
        if not eid or r["accepted"] is None:
            unattributed += 1
            continue
        e = events.setdefault(eid, {"market": r["market"], "variants": Counter(), "traded": 0, "engine_accepted": 0})
        e["variants"][r.get("variant") or "?"] += 1
        if r["accepted"]:
            e["engine_accepted"] += 1
        if r["state"] in TRADED_STATES and not r["stack_reject_code"]:
            e["traded"] += 1
    per_variant: Counter[str] = Counter()
    for e in events.values():
        per_variant.update(e["variants"])
    multi = sum(1 for e in events.values() if len(e["variants"]) > 1)
    return {
        "raw_signals": sum(per_variant.values()),
        "structure_events": len(events),
        "events_with_multiple_variants": multi,
        "signals_per_event_mean": (sum(per_variant.values()) / len(events)) if events else None,
        "events_with_a_trade": sum(1 for e in events.values() if e["traded"] > 0),
        "raw_by_variant": dict(sorted(per_variant.items())),
        "events_by_variant": dict(sorted(Counter(v for e in events.values() for v in e["variants"]).items())),
        "unattributed_signals": unattributed,
        "note": "signals sharing a structure_event_id react to ONE break and are highly related: use event-level counts, "
                "not raw signal counts, as the sample size of any per-variant or pooled statistic",
    }


def stack_stage(base: str, violated_cap: str | None = None) -> str:
    if base == G.R_SIZE_BELOW_MIN and violated_cap in _PORTFOLIO_CAPS:
        return "PORTFOLIO"
    return _STACK_STAGE.get(base, "EXECUTABLE")


def _classify_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """{kind, codes, fail_stage (None = not blocked), reached (index of the last stage passed)} per
    opportunity; None for a snapshot without a decision."""
    acc = row["accepted"]
    if acc is None:
        return None
    if acc is False:
        codes = [r for r in row["reasons"] if r != "ACCEPTED"]
        idx = [_SI[_ENGINE_STAGE.get(c, "TRADABLE")] for c in codes] or [_SI["TRADABLE"]]
        k = min(idx)
        kind = "CATCHUP_MISSED" if "CATCHUP_MISSED" in codes else "ENGINE_REJECTED"
        return {"kind": kind, "codes": codes, "fail_stage": STAGES[k], "reached": k - 1}
    state, code = row["state"], row["stack_reject_code"]
    rejected = state == "RISK_REJECTED" or bool(code)
    if row.get("shadow_dry_run") and not rejected:
        return {"kind": "SHADOW_WOULD_TRADE", "codes": [], "fail_stage": None, "reached": _SI["ACCEPTED"]}
    if state in TRADED_STATES and not rejected:
        return {"kind": "TRADED", "codes": [], "fail_stage": None, "reached": _SI["EXECUTED"]}
    if rejected:
        base = _base(code) if code else "unknown"
        st = stack_stage(base, row.get("violated_cap"))
        return {"kind": "STACK_REJECTED", "codes": [base], "fail_stage": st, "reached": _SI[st] - 1}
    if state == "CANCELLED":
        reason = row.get("cancel_reason") or "cancelled"
        return {"kind": "CANCELLED", "codes": [f"CANCELLED:{reason}"], "fail_stage": "EXECUTABLE",
                "reached": _SI["EXECUTABLE"] - 1}
    if state == "SEND_FAILED":
        return {"kind": "SEND_FAILED", "codes": ["SEND_FAILED"], "fail_stage": "EXECUTABLE",
                "reached": _SI["EXECUTABLE"] - 1}
    if row["approved"] is True:
        return {"kind": "APPROVED_IN_FLIGHT", "codes": [], "fail_stage": None, "reached": _SI["ACCEPTED"]}
    return {"kind": "PENDING", "codes": [], "fail_stage": None, "reached": _SI["DUPLICATE"]}


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def cf_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Counterfactual stats of a group of labels (optimistic fill at the intended entry, no costs)."""
    resolved = [r for r in rows if r["target_before_stop"] is not None]
    return {
        "n_labelled": len(rows),
        "mean_mfe_r": _mean([r["mfe_r"] for r in rows]),
        "mean_mae_r": _mean([r["mae_r"] for r in rows]),
        "mean_r": _mean([r["r"] for r in rows]),
        "n_resolved": len(resolved),
        "target_before_stop_share": (sum(1 for r in resolved if r["target_before_stop"]) / len(resolved)) if resolved else None,
    }


def _gate_layer(kind: str) -> str:
    return {"ENGINE_REJECTED": "engine", "CATCHUP_MISSED": "catchup", "STACK_REJECTED": "stack"}.get(kind, "operational")


def _gate_class(code: str, kind: str) -> str:
    if kind in ("ENGINE_REJECTED", "CATCHUP_MISSED"):
        return "CATCHUP" if code in ("CATCHUP_MISSED", "EXPIRED_ENTRY", "ALREADY_MOVED") else engine_class(code)
    if kind == "STACK_REJECTED":
        return stack_class(code)
    return "OPERATIONAL"


def _hour(row: Mapping[str, Any]) -> str:
    m = row.get("local_minute")
    return "?" if m is None else f"{int(m) // 60:02d}"


def analysis(rows: list[Mapping[str, Any]], cf_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Stages, special buckets, per-gate slices (market / family / session / local hour) with counterfactual
    stats, the opportunities-per-market-per-hour table, family / market / cluster shares and the
    same-structure duplicate indicator (true frequency vs near-duplicates)."""
    from demo.execution.risk_policy import cluster_of

    cls: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for r in rows:
        c = _classify_row(r)
        if c is not None:
            cls.append((r, c))
    n = len(cls)
    stages = []
    for i, st in enumerate(STAGES):
        passed = sum(1 for _r, c in cls if c["reached"] >= i)
        blocked = sum(1 for _r, c in cls if c["fail_stage"] == st)
        stages.append({"stage": st, "passed": passed, "blocked_here": blocked, "share_of_raw": (passed / n) if n else None})
    gates: dict[str, dict[str, Any]] = {}
    for r, c in cls:
        for code in c["codes"]:
            g = gates.setdefault(code, {
                "layer": _gate_layer(c["kind"]), "class": _gate_class(code, c["kind"]), "count": 0, "exclusive_count": 0,
                "by_market": Counter(), "by_family": Counter(), "by_session": Counter(), "by_hour": Counter(),
            })
            g["count"] += 1
            if len(c["codes"]) == 1:
                g["exclusive_count"] += 1
            g["by_market"][r["market"] or "?"] += 1
            g["by_family"][r["family"] or "?"] += 1
            g["by_session"][r.get("session") or "?"] += 1
            g["by_hour"][_hour(r)] += 1
    by_code_cf: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_source_cf: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cf in cf_rows:
        by_source_cf[cf["source"]].append(cf)
        for code in cf["gate_codes"] or ([cf["gate_code"]] if cf["gate_code"] else []):
            by_code_cf[code].append(cf)
    for code, g in gates.items():
        g["share_of_opportunities"] = (g["count"] / n) if n else None
        for k in ("by_market", "by_family", "by_session", "by_hour"):
            g[k] = dict(sorted(g[k].items()))
        g["counterfactual"] = cf_stats(by_code_cf.get(code, []))
    for code, lst in by_code_cf.items():  # labelled gates that no longer appear in the live rows
        gates.setdefault(code, {"layer": "?", "class": "?", "count": 0, "exclusive_count": 0, "share_of_opportunities": None,
                                "by_market": {}, "by_family": {}, "by_session": {}, "by_hour": {}, "counterfactual": cf_stats(lst)})
    special: dict[str, int] = {}
    for name, codes in SPECIAL_BUCKETS.items():
        special[name] = sum(1 for _r, c in cls if any(x in codes for x in c["codes"]))
    per_hour: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    days: dict[str, set[str]] = defaultdict(set)
    for r, _c in cls:
        per_hour[r["market"] or "?"][_hour(r)] += 1
        if r.get("signal_ts"):
            days[r["market"] or "?"].add(str(r["signal_ts"])[:10])
    table = {
        m: {"opportunities_by_local_hour": dict(sorted(h.items())), "active_days": len(days.get(m, ())),
            "per_active_day_by_local_hour": {k: v / max(1, len(days.get(m, ()))) for k, v in sorted(h.items())}}
        for m, h in sorted(per_hour.items())
    }

    def share(key: Any, sub: list[tuple[Mapping[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        cnt = Counter(key(r) for r, _c in sub)
        tot = sum(cnt.values())
        return {k: {"n": v, "share": v / tot} for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])} if tot else {}

    seen_struct: dict[tuple, int] = {}
    uniq: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for r, c in cls:
        atr, stop = r.get("atr"), r.get("stop")
        if atr and stop is not None and atr > 0:
            zone = round(float(stop) / (STRUCTURE_ATR_STEP * float(atr)))
        else:
            zone = None if stop is None else round(float(stop), 2)
        key = (r["market"], r["family"], r.get("direction"), zone, str(r.get("signal_ts") or "")[:10])
        if key not in seen_struct:
            seen_struct[key] = 0
            uniq.append((r, c))
        seen_struct[key] += 1
    dup_by_market: dict[str, dict[str, Any]] = {}
    for m in sorted({r["market"] or "?" for r, _c in cls}):
        tot = sum(1 for r, _c in cls if (r["market"] or "?") == m)
        un = sum(1 for r, _c in uniq if (r["market"] or "?") == m)
        dup_by_market[m] = {"opportunities": tot, "unique_structures": un, "near_duplicate_ratio": (1 - un / tot) if tot else None}
    shares = {
        "raw": {
            "family": share(lambda r: r["family"] or "?", cls), "market": share(lambda r: r["market"] or "?", cls),
            "cluster": share(lambda r: cluster_of(r["market"] or "") or "?", cls),
        },
        "unique_structures": {
            "family": share(lambda r: r["family"] or "?", uniq), "market": share(lambda r: r["market"] or "?", uniq),
            "cluster": share(lambda r: cluster_of(r["market"] or "") or "?", uniq),
        },
    }
    return {
        "stages": stages,
        "special_buckets": special,
        "gates": dict(sorted(gates.items(), key=lambda kv: -kv[1]["count"])),
        "counterfactual_by_source": {k: cf_stats(v) for k, v in sorted(by_source_cf.items())},
        "counterfactual_fill_assumption": "intended entry, no slippage/fees/latency (OPTIMISTIC)",
        "opportunities_per_market_per_hour": table,
        "shares": shares,
        "structure_duplicates": {
            "step_atr": STRUCTURE_ATR_STEP,
            "key": "market+family+direction+stop zone (0.25 ATR)+UTC day",
            "opportunities": n, "unique_structures": len(uniq),
            "near_duplicate_ratio": (1 - len(uniq) / n) if n else None, "by_market": dup_by_market,
        },
        "arbitration": arbitration_counts([r for r, _c in cls]),
        "structure_events": structure_event_clusters([r for r, _c in cls]),
        "note_outside_entry_window": "OUTSIDE_ENTRY_WINDOW is not observable on the tradable path (family entry_mask); see the OUT_OF_WINDOW_SHADOW section (Lane U2, measurement only)",
        "origin": dict(Counter(r.get("origin") or "LIVE" for r, _c in cls)),
    }


def _by(rows: list[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(r.get(key) or "?") for r in rows).items(), key=lambda kv: -kv[1]))


def out_of_window_section(rows: list[Mapping[str, Any]], cf_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Lane U2: what the frozen families WOULD have signalled while the broker was tradable but the entry window was
    closed (``OUT_OF_WINDOW_SHADOW``, class WINDOW). Measurement only - never a trade, never a window change."""
    mine = [r for r in rows if OUT_OF_WINDOW_SHADOW in (r.get("reasons") or [])]
    cfs = [c for c in cf_rows if c.get("source") == OUT_OF_WINDOW_SHADOW]
    by_m: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for c in cfs:
        by_m[c["market"] or "?"].append(c)
    return {
        "gate_class": "WINDOW", "code": OUT_OF_WINDOW_SHADOW, "stage": "STRATEGY_WINDOW",
        "recorded": len(mine), "by_market": _by(mine, "market"), "by_family": _by(mine, "family"),
        "counterfactual": cf_stats(cfs),
        "counterfactual_by_market": {m: cf_stats(v) for m, v in sorted(by_m.items())},
        "counterfactual_fill_assumption": "intended entry, no slippage/fees/latency (OPTIMISTIC)",
        "note": "no order is ever sent; active windows are NOT widened automatically",
    }


def shadow_universe_section(rows: list[Mapping[str, Any]], cf_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Lane U2: shadow-only markets (code ``SHADOW_UNIVERSE``), by market / cluster / family with counterfactual stats."""
    mine = [r for r in rows if r.get("origin") == SHADOW_UNIVERSE]
    cfs = [c for c in cf_rows if c.get("source") == SHADOW_UNIVERSE]
    by_c: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for c in cfs:
        by_c[c.get("cluster") or "?"].append(c)
    return {
        "gate_class": "SHADOW_UNIVERSE", "code": SHADOW_UNIVERSE,
        "recorded": len(mine), "markets": len({r["market"] for r in mine}),
        "by_market": _by(mine, "market"), "by_cluster": _by(mine, "cluster"), "by_family": _by(mine, "family"),
        "counterfactual": cf_stats(cfs),
        "counterfactual_by_cluster": {k: cf_stats(v) for k, v in sorted(by_c.items())},
        "note": "shadow-only markets are never tradable; excluded from every trading metric above",
    }


def sequence_section(rows: list[Mapping[str, Any]], cf_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    from demo.sequence_metrics import summarize

    return summarize([dict(r) for r in rows], {c["opportunity_id"]: c for c in cf_rows})


def compact(full: Mapping[str, Any]) -> dict[str, Any]:
    """Heartbeat-sized funnel: stage pass counts, special buckets, top blocking gates with counterfactual R."""
    a = full.get("analysis") or {}
    gates = a.get("gates", {})
    return {
        "stages": {s["stage"]: s["passed"] for s in a.get("stages", [])},
        "special_buckets": a.get("special_buckets", {}),
        "top_gates": {
            k: {"n": g["count"], "cf_n": g["counterfactual"]["n_labelled"], "cf_mean_r": g["counterfactual"]["mean_r"]}
            for k, g in list(gates.items())[:6]
        },
        "near_duplicate_ratio": (a.get("structure_duplicates") or {}).get("near_duplicate_ratio"),
        "arbitration": a.get("arbitration", {}),
        "structure_events": {k: (a.get("structure_events") or {}).get(k)
                             for k in ("raw_signals", "structure_events", "events_with_a_trade")},
    }


def funnel(store: Any, stack: Any | None = None, phase: str | None = None) -> dict[str, Any]:
    """Rejection funnel over everything recorded in ``store`` (optionally one ``phase``).

    ``stack`` (optional) contributes ``stack_live``: the in-process counters of ``rejection_funnel()``
    (cumulative since the stack was constructed, includes rejections whose intent row is not
    persisted). The store part is the durable one."""
    all_rows = [r for r in store.funnel_rows(phase) if r.get("trade_type", "STRATEGY") == "STRATEGY"]
    # Lane U2: shadow-universe markets are not part of the trading funnel (they would swamp every share); they get their own section.
    rows = [r for r in all_rows if r.get("origin") != SHADOW_UNIVERSE]
    cf_fn = getattr(store, "counterfactual_rows", None)
    all_cf = cf_fn(phase) if cf_fn is not None else []
    cf_rows = [c for c in all_cf if c.get("source") != SHADOW_UNIVERSE]
    total = _Bucket()
    by_market: dict[str, _Bucket] = {}
    by_family: dict[str, _Bucket] = {}
    for row in rows:
        total.add(row)
        by_market.setdefault(row["market"] or "?", _Bucket()).add(row)
        by_family.setdefault(row["family"] or "?", _Bucket()).add(row)
    states: Counter[str] = Counter(r["state"] for r in rows if r["state"])
    live: dict[str, Any] | None = None
    if stack is not None:
        fn = getattr(stack, "rejection_funnel", None)
        if fn is not None:
            try:
                live = fn()
            except Exception as exc:  # a diagnostics read must never break the runner
                live = {"error": f"{type(exc).__name__}: {exc}"}
    top = total.as_dict()
    temp_detail = {
        "total": total.stack_by_class.get("TEMPORARY", 0),
        "otherwise_valid_blocked": total.temporary_blocked,
        "by_code": dict(total.temporary_codes),
        "lifting_conditions": {
            c: g.lifting_condition
            for c in total.temporary_codes
            if (g := G.GATE_CATALOG.get(c)) is not None and g.lifting_condition
        },
    }
    return {
        "phase": phase or "ALL",
        "summary": {
            "opportunities": top["opportunities"],
            "engine_accepted": top["engine_accepted"],
            "engine_rejected": top["engine_rejected"],
            "stack_rejected": top["stack_rejected"],
            "traded": top["traded"],
            "shadow_would_trade": top["shadow_would_trade"],
            "temporary_otherwise_valid_blocked": top["temporary_otherwise_valid_blocked"],
            "trades_that_would_have_existed": top["trades_that_would_have_existed"],
            "engine_top_reasons": dict(list(top["engine_reasons"].items())[:5]),
            "stack_top_codes": dict(list(top["stack_codes"].items())[:5]),
        },
        "engine": {
            "by_reason": top["engine_reasons"],
            "by_class": top["engine_by_class"],
            "classification": {c: {"class": g.gate_class, "hard": g.hard, "why": g.why} for c, g in GATE_CLASSIFICATION.items()},
            "rejected_only_by_non_hard_classes": top["engine_rejected_only_by_non_hard_classes"],
        },
        "stack": {
            "by_class": top["stack_by_class"],
            "by_code": top["stack_codes"],
            "temporary_limitation": temp_detail,
            "intent_states": dict(states),
        },
        "trades_that_would_have_existed": top["trades_that_would_have_existed"],
        "by_market": {m: b.as_dict() for m, b in sorted(by_market.items())},
        "by_family": {f: b.as_dict() for f, b in sorted(by_family.items())},
        "stack_live": live,
        "analysis": analysis(rows, cf_rows),
        "out_of_window_shadow": out_of_window_section(rows, all_cf),
        "shadow_universe": shadow_universe_section(all_rows, all_cf),
        "signal_sequence": sequence_section(all_rows, all_cf),
    }


def _f(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def render(fun: Mapping[str, Any]) -> str:
    """Compact human-readable rendering (milestone report / --analyze)."""
    s = fun["summary"]
    w = s["trades_that_would_have_existed"]
    lines = [
        f"REJECTION FUNNEL [{fun['phase']}]",
        f"  opportunities {s['opportunities']} | engine accepted {s['engine_accepted']} | engine rejected {s['engine_rejected']}"
        f" | stack rejected {s['stack_rejected']} | traded {s['traded']} | shadow would-trade {s['shadow_would_trade']}",
        f"  trades that would have existed: actual {w['actual']} | w/o TEMPORARY {w['without_temporary_limitations']}"
        f" | w/o TEMPORARY+LEGACY stack {w['without_temporary_and_legacy_stack_gates']}"
        f" | engine-side w/o LEGACY/QUALITY {w['engine_side_without_legacy_quality_temporary']}",
        "  engine reasons: " + (", ".join(f"{k}={v}" for k, v in fun["engine"]["by_reason"].items()) or "-"),
        "  engine by class: " + (", ".join(f"{k}={v}" for k, v in fun["engine"]["by_class"].items()) or "-"),
        "  stack by class: " + (", ".join(f"{k}={v}" for k, v in fun["stack"]["by_class"].items()) or "-"),
        "  stack codes: " + (", ".join(f"{k}={v}" for k, v in fun["stack"]["by_code"].items()) or "-"),
        f"  TEMPORARY otherwise-valid blocked: {fun['stack']['temporary_limitation']['otherwise_valid_blocked']}",
    ]
    for label, key in (("market", "by_market"), ("family", "by_family")):
        for name, b in fun[key].items():
            lines.append(
                f"  {label} {name}: opp {b['opportunities']} acc {b['engine_accepted']} rej {b['engine_rejected']}"
                f" stack_rej {b['stack_rejected']} traded {b['traded']} shadow_would {b['shadow_would_trade']} temp_blocked {b['temporary_otherwise_valid_blocked']}"
            )
    a = fun.get("analysis")
    if a:
        lines.append("  STAGES: " + " -> ".join(f"{s['stage']} {s['passed']}" for s in a["stages"]))
        lines.append("  SPECIAL: " + ", ".join(f"{k}={v}" for k, v in a["special_buckets"].items()))
        lines.append("  GATES (count share | counterfactual n meanMFE_R meanMAE_R meanR tbs-share; optimistic fill):")

        def f(v: float | None) -> str:
            return "-" if v is None else f"{v:.2f}"

        for code, g in a["gates"].items():
            cf = g["counterfactual"]
            lines.append(
                f"    {code} [{g['layer']}/{g['class']}] n={g['count']} ({f(g['share_of_opportunities'])}) excl={g['exclusive_count']}"
                f" | cf n={cf['n_labelled']} mfe={f(cf['mean_mfe_r'])} mae={f(cf['mean_mae_r'])} r={f(cf['mean_r'])} tbs={f(cf['target_before_stop_share'])}"
            )
        sd = a["structure_duplicates"]
        lines.append(f"  STRUCTURE: {sd['opportunities']} opportunities / {sd['unique_structures']} unique ({sd['key']}); near-duplicate ratio {sd['near_duplicate_ratio']}")
        for kind in ("raw", "unique_structures"):
            fam = ", ".join(f"{k}={v['share']:.0%}" for k, v in a["shares"][kind]["family"].items())
            lines.append(f"  family share [{kind}]: {fam or '-'}")
        for m, t in a["opportunities_per_market_per_hour"].items():
            lines.append(f"  {m} per local hour: " + ", ".join(f"{h}h={v}" for h, v in t["opportunities_by_local_hour"].items()) + f" (active days {t['active_days']})")
        arb = a.get("arbitration")
        if arb:
            lines.append("  ARBITRATION (symbol occupied, MT5 netting): " + ", ".join(f"{k}={v}" for k, v in arb.items()))
        se = a.get("structure_events")
        if se and se["structure_events"]:
            lines.append(
                f"  STRUCTURE EVENTS: {se['raw_signals']} raw signals / {se['structure_events']} events "
                f"({se['events_with_multiple_variants']} with >1 variant, {se['events_with_a_trade']} with a trade); "
                f"raw by variant {se['raw_by_variant']}, events by variant {se['events_by_variant']}; {se['note']}"
            )
        lines.append("  NOTE: " + a["note_outside_entry_window"])
    oow = fun.get("out_of_window_shadow")
    if oow:
        cf = oow["counterfactual"]
        lines.append(
            f"  OUT_OF_WINDOW_SHADOW [WINDOW, measurement only]: recorded {oow['recorded']} | cf n={cf['n_labelled']} "
            f"mfe={_f(cf['mean_mfe_r'])} mae={_f(cf['mean_mae_r'])} r={_f(cf['mean_r'])} tbs={_f(cf['target_before_stop_share'])}"
        )
    su = fun.get("shadow_universe")
    if su:
        cf = su["counterfactual"]
        lines.append(
            f"  SHADOW_UNIVERSE: recorded {su['recorded']} over {su['markets']} markets | cf n={cf['n_labelled']} "
            f"mfe={_f(cf['mean_mfe_r'])} mae={_f(cf['mean_mae_r'])} r={_f(cf['mean_r'])} tbs={_f(cf['target_before_stop_share'])}"
        )
    sq = fun.get("signal_sequence")
    if sq and sq["signals_with_sequence"]:
        lines.append(
            f"  SEQUENCE (measure only): n={sq['signals_with_sequence']} same-zone {sq['same_zone_reengagement']['n']}"
            f" repeated-level {sq['repeated_level_attempt']['n']} flips {sq['direction_flip']['n']} whipsaw {sq['whipsaw']['n']}"
            f" | whipsaw mean_r={_f(sq['whipsaw']['mean_r'])} mfe={_f(sq['whipsaw']['mean_mfe_r'])} mae={_f(sq['whipsaw']['mean_mae_r'])}"
        )
    return "\n".join(lines)
