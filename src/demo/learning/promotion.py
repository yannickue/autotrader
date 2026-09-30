# ruff: noqa: E501
"""Promotion gate report: ADVISORY ONLY. There is NO automatic promotion.

`promotion_report` is a pure function. It evaluates a fixed list of gates against the evidence it is
given and returns a report whose `auto_promote` is ALWAYS False (a module constant, not a
threshold). The most the report can say is `eligible_for_manual_review`; a human owner must decide,
and even then a promotion is only a recorded tag (`tracking.LearningTracker.record_role_change`)
that nothing in the trading path reads. Missing evidence FAILS a gate (fail closed).

Gates: enough samples, positive net expectancy, better than champion, chronological validation,
calibration, permutation baseline, no leakage, not a single market/session/family cluster,
acceptable drawdown, and separate forward evidence (a later FROZEN window disjoint from training).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

AUTO_PROMOTE = False  # constant by design; there is no code path that sets it to True


@dataclass(frozen=True)
class PromotionThresholds:
    min_samples: int = 300
    min_net_expectancy_r: float = 0.0
    min_edge_over_champion_r: float = 0.05
    min_folds: int = 3
    max_ece: float = 0.10
    max_permutation_p: float = 0.05
    max_cluster_share: float = 0.60
    max_drawdown_r: float = 8.0
    min_forward_trades: int = 100
    min_forward_expectancy_r: float = 0.0


def _ok(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _gate(name: str, passed: bool, value: Any, threshold: Any, detail: str) -> dict[str, Any]:
    return {"gate": name, "passed": bool(passed), "value": value, "threshold": threshold, "detail": detail}


def promotion_report(
    candidate: dict[str, Any],
    champion: dict[str, Any] | None = None,
    forward: dict[str, Any] | None = None,
    thresholds: PromotionThresholds | None = None,
) -> dict[str, Any]:
    """Evaluate the gates.

    candidate: n_samples, net_expectancy_r, n_folds, chronological_validation (bool),
      ece, brier, brier_base_rate, permutation_p, leakage_check_passed (bool),
      max_market_share, max_session_share, max_family_share, max_drawdown_r,
      training_end_utc (ISO).
    champion: net_expectancy_r (static demo policy on the same window) or None.
    forward:  phase ("FROZEN"), n_trades, net_expectancy_r, start_utc (ISO) or None.
    """
    t = thresholds or PromotionThresholds()
    c, ch, fw = candidate or {}, champion or {}, forward or {}
    gates: list[dict[str, Any]] = []

    n = c.get("n_samples")
    gates.append(_gate("enough_samples", _ok(n) and n >= t.min_samples, n, t.min_samples,
                       "closed real trades used for validation"))
    ne = c.get("net_expectancy_r")
    gates.append(_gate("positive_net_expectancy", _ok(ne) and ne > t.min_net_expectancy_r, ne,
                       t.min_net_expectancy_r, "out-of-fold net expectancy (R) of the model-selected trades"))
    che = ch.get("net_expectancy_r")
    edge = (ne - che) if _ok(ne) and _ok(che) else None
    gates.append(_gate("better_than_champion", edge is not None and edge >= t.min_edge_over_champion_r,
                       edge, t.min_edge_over_champion_r,
                       "candidate minus static-policy champion expectancy (R); missing champion fails"))
    nf = c.get("n_folds")
    gates.append(_gate("chronological_validation",
                       c.get("chronological_validation") is True and _ok(nf) and nf >= t.min_folds,
                       {"flag": c.get("chronological_validation"), "n_folds": nf}, t.min_folds,
                       "expanding-window, purged, embargoed folds"))
    ece, br, bb = c.get("ece"), c.get("brier"), c.get("brier_base_rate")
    gates.append(_gate("calibration", _ok(ece) and _ok(br) and _ok(bb) and ece <= t.max_ece and br < bb,
                       {"ece": ece, "brier": br, "brier_base_rate": bb}, t.max_ece,
                       "ECE bounded and Brier better than the base-rate forecast"))
    pp = c.get("permutation_p")
    gates.append(_gate("permutation_baseline", _ok(pp) and pp <= t.max_permutation_p, pp,
                       t.max_permutation_p, "AUC beats shuffled-label baseline"))
    gates.append(_gate("no_leakage", c.get("leakage_check_passed") is True, c.get("leakage_check_passed"),
                       True, "feature whitelist + timing guards verified"))
    shares = {k: c.get(f"max_{k}_share") for k in ("market", "session", "family")}
    gates.append(_gate("not_single_cluster",
                       all(_ok(v) and v <= t.max_cluster_share for v in shares.values()), shares,
                       t.max_cluster_share, "no single market/session/family carries the positive R"))
    dd = c.get("max_drawdown_r")
    gates.append(_gate("drawdown_acceptable", _ok(dd) and dd <= t.max_drawdown_r, dd, t.max_drawdown_r,
                       "max drawdown (R) of the selected trades"))
    fwd_ok = (
        fw.get("phase") == "FROZEN"
        and _ok(fw.get("n_trades")) and fw["n_trades"] >= t.min_forward_trades
        and _ok(fw.get("net_expectancy_r")) and fw["net_expectancy_r"] > t.min_forward_expectancy_r
        and isinstance(fw.get("start_utc"), str) and isinstance(c.get("training_end_utc"), str)
        and fw["start_utc"] > c["training_end_utc"]
    )
    gates.append(_gate("separate_forward_evidence", fwd_ok,
                       {k: fw.get(k) for k in ("phase", "n_trades", "net_expectancy_r", "start_utc")},
                       t.min_forward_trades,
                       "later FROZEN window, disjoint from training, own positive net expectancy"))

    failed = [g["gate"] for g in gates if not g["passed"]]
    return {
        "advisory": True,
        "auto_promote": AUTO_PROMOTE,
        "all_gates_passed": not failed,
        "failed_gates": failed,
        "recommendation": "eligible_for_manual_review" if not failed else "not_eligible",
        "gates": gates,
        "note": "Advisory only. Promotion is a manual owner decision; nothing is promoted automatically.",
    }
