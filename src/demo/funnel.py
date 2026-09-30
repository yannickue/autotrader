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

from collections import Counter
from collections.abc import Mapping
from typing import Any

from demo.execution import gates as G
from demo.opportunity.policy import GATE_CLASSIFICATION

CLASSES = ("SAFETY", "STRUCTURAL", "TEMPORARY", "QUALITY", "LEGACY_ARBITRARY")
NON_HARD_ENGINE = frozenset({"LEGACY_ARBITRARY", "QUALITY", "TEMPORARY"})
TRADED_STATES = frozenset({"FILLED", "PROTECTED", "CLOSED"})
UNCLASSIFIED = "UNCLASSIFIED"


def engine_class(code: str) -> str:
    g = GATE_CLASSIFICATION.get(code)
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


def funnel(store: Any, stack: Any | None = None, phase: str | None = None) -> dict[str, Any]:
    """Rejection funnel over everything recorded in ``store`` (optionally one ``phase``).

    ``stack`` (optional) contributes ``stack_live``: the in-process counters of ``rejection_funnel()``
    (cumulative since the stack was constructed, includes rejections whose intent row is not
    persisted). The store part is the durable one."""
    rows = store.funnel_rows(phase)
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
    }


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
    return "\n".join(lines)
