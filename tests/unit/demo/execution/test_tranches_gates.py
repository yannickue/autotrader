# ruff: noqa: E501
"""Tranche ledger, add-on classification and the gate catalogue."""

from __future__ import annotations

import ast
from decimal import Decimal as D
from pathlib import Path

import pytest

from demo.execution import gates as G
from demo.execution.tranches import (
    AddonClass,
    Tranche,
    TrancheLedger,
    classify_addon,
)


def tr(intent_id, market, cluster, family, direction, qty, entry, stop, target, risk) -> Tranche:
    return Tranche(
        intent_id=intent_id, market=market, cluster=cluster, family=family, direction=direction,
        quantity=D(qty), entry_price=D(entry), stop=D(stop), target=None if target is None else D(target),
        risk_money=D(risk),
    )


# -- tranche ledger --------------------------------------------------------------------------------------


def test_ledger_aggregates_tranches_into_net_positions_and_risk_books():
    ledger = TrancheLedger.of(
        [
            tr("a", "NAS100", "INDEX", "BREAKOUT", 1, "1.0", "21000", "20950", "21150", 50),
            tr("b", "NAS100", "INDEX", "REVERSAL", 1, "0.6", "21020", "20950", "21150", 42),
            tr("c", "SPX500", "INDEX", "BREAKOUT", 1, "2.0", "6000", "5980", "6050", 40),
            tr("d", "XAUUSD", "METAL", "BREAKOUT", -1, "0.05", "4170", "4190", None, 90),
        ]
    )
    assert ledger.total_risk() == 222
    assert ledger.risk_by_market() == {"NAS100": 92, "SPX500": 40, "XAUUSD": 90}
    assert ledger.risk_by_cluster() == {"INDEX": 132, "METAL": 90}  # NASDAQ + SPX = ONE cluster
    assert ledger.risk_by_family() == {"BREAKOUT": 180, "REVERSAL": 42}
    nas = ledger.net_position("NAS100")
    assert nas is not None and len(nas.tranches) == 2  # two intents, ONE broker net position
    assert nas.net_quantity == D("1.6") and nas.risk_money == 92
    assert nas.average_entry == (D("1.0") * 21000 + D("0.6") * 21020) / D("1.6")
    assert ledger.net_position("XAUUSD").net_quantity == D("-0.05")
    shares = ledger.shares()
    assert shares["cluster"]["INDEX"] == D(132) / D(222)
    assert shares["family"]["BREAKOUT"] == D(180) / D(222)
    assert sum(shares["market"].values()) == 1


def test_ledger_close_removes_only_that_tranche_and_rejects_duplicates():
    ledger = TrancheLedger.of([tr("a", "NAS100", "INDEX", "F", 1, "1", "1", "0.5", None, 10),
                               tr("b", "NAS100", "INDEX", "F", 1, "1", "1", "0.6", None, 5)])
    with pytest.raises(ValueError, match="already"):
        ledger.add(tr("a", "NAS100", "INDEX", "F", 1, "1", "1", "0.5", None, 10))
    assert ledger.close("a") is not None and ledger.total_risk() == 5
    assert ledger.close("zzz") is None
    assert TrancheLedger().shares()["market"] == {}


def test_single_stop_protecting_all_tranches_is_the_tightest_structural_stop():
    long = TrancheLedger.of([tr("a", "X", "C", "F", 1, "1", "100", "90", None, 10),
                             tr("b", "X", "C", "F", 1, "1", "100", "95", None, 5)]).net_position("X")
    assert long.single_stop_protecting_all() == 95  # any single stop must be the tightest
    short = TrancheLedger.of([tr("a", "X", "C", "F", -1, "1", "100", "110", None, 10),
                              tr("b", "X", "C", "F", -1, "1", "100", "105", None, 5)]).net_position("X")
    assert short.single_stop_protecting_all() == 105


# -- add-on classification (shared stop vs independent stops) -----------------------------------------------------


def classify(**kw):
    base = dict(
        existing_direction=1, existing_stop=D("24950"), existing_target=D("25150"),
        new_direction=1, new_stop=D("24950"), new_target=D("25150"),
        executable_price=D("25001.5"), tick_size=D("0.01"),
    )
    base.update(kw)
    return classify_addon(**base)


def test_identical_stop_and_target_can_share_the_existing_stop():
    result = classify()
    assert result.classification is AddonClass.SHARED_STOP_POSSIBLE


def test_a_tighter_existing_stop_within_tolerance_can_be_shared_but_never_a_looser_one():
    # new stop 24948 (d = 53.5); existing 24950 is 2 points TIGHTER (3.7 % of d): shared is fine
    ok = classify(new_stop=D("24948"))
    assert ok.classification is AddonClass.SHARED_STOP_POSSIBLE
    assert ok.tightening_fraction == D(2) / D("53.5")
    # new stop 24955 is TIGHTER than the existing 24950: the existing stop would be LOOSER
    looser = classify(new_stop=D("24955"))
    assert looser.classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert "LOOSER" in looser.why  # never accept more risk than the new setup allows


def test_a_materially_tighter_shared_stop_is_a_different_trade():
    result = classify(existing_stop=D("24990"), new_stop=D("24950"))  # 40 pts tighter, d = 51.5
    assert result.classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert "materially tighter" in result.why
    assert classify(existing_stop=D("24990"), new_stop=D("24950"),
                    max_tightening_fraction=D("0.9")).classification is AddonClass.SHARED_STOP_POSSIBLE


def test_targets_must_match_because_one_position_tp_cannot_serve_two_exits():
    assert classify(new_target=D("25300")).classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert classify(new_target=None).classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert classify(existing_target=None, new_target=None).classification is AddonClass.SHARED_STOP_POSSIBLE


def test_unprotected_or_crossed_existing_stops_and_opposite_side():
    assert classify(existing_stop=None).classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert classify(existing_stop=D("25100")).classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    assert classify(new_direction=-1).classification is AddonClass.OPPOSITE_SIDE


def test_short_side_mirrors_the_long_rules():
    base = dict(existing_direction=-1, new_direction=-1, executable_price=D("25000"),
                existing_stop=D("25050"), new_stop=D("25050"), existing_target=D("24850"),
                new_target=D("24850"))
    assert classify(**base).classification is AddonClass.SHARED_STOP_POSSIBLE
    looser_existing = {**base, "existing_stop": D("25080")}  # existing stop ABOVE = looser for a short
    assert classify(**looser_existing).classification is AddonClass.INDEPENDENT_STOPS_NEEDED
    tighter_ok = {**base, "existing_stop": D("25048")}
    assert classify(**tighter_ok).classification is AddonClass.SHARED_STOP_POSSIBLE


# -- gate catalogue ---------------------------------------------------------------------------------------------------


def test_every_reason_constant_is_in_the_catalogue_with_a_class_and_a_hard_flag():
    codes = {v for k, v in vars(G).items() if k.startswith("R_") and isinstance(v, str)}
    assert codes, "no reason constants found"
    for code in codes:
        gate = G.GATE_CATALOG.get(code)
        assert gate is not None, code
        assert isinstance(gate.gate_class, G.GateClass) and isinstance(gate.hard, bool) and gate.why


def test_quality_gates_never_hard_reject_and_temporary_gates_name_their_lifting_condition():
    for gate in G.GATE_CATALOG.values():
        if gate.gate_class is G.GateClass.QUALITY:
            assert gate.hard is False and gate.emits == "log_only", gate.code
        if gate.gate_class is G.GateClass.TEMPORARY:
            assert gate.lifting_condition and gate.hard is True, gate.code
    assert {g.code for g in G.GATE_CATALOG.values() if g.gate_class is G.GateClass.QUALITY} >= {
        "confidence", "confluence", "family_score", "quality_components"
    }
    netting = G.GATE_CATALOG["NETTING_ONE_NET_POSITION_PER_SYMBOL"]
    assert netting.gate_class is G.GateClass.STRUCTURAL and netting.hard is False and netting.emits == "none"
    assert "NOT a one-opportunity-per-symbol rule" in netting.why


def test_temporary_limitations_are_exactly_the_three_named_codes():
    temporary = {g.code for g in G.GATE_CATALOG.values() if g.gate_class is G.GateClass.TEMPORARY}
    assert temporary == {
        "ADDON_EXPOSURE_NOT_SUPPORTED_V1",
        "ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED",
        "OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1",
    }


def _codes_emitted_in(path: Path) -> set[str]:
    """Every reason string a module can emit: literals / G.R_* / REASON_* handed to the reject
    helpers or returned from the parity, pre-reject and refusal-mapping functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_consts = {
        t.id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    found: set[str] = set()

    def resolve(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "G":
            found.add(getattr(G, node.attr))
        elif isinstance(node, ast.Name) and node.id in module_consts:
            found.add(module_consts[node.id])
        elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.FormattedValue):
            resolve(node.values[0].value)
        elif isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
            found.add(str(node.values[0].value).split(":")[0])

    emitters = {"_Reject", "_reject", "_skip", "_reject_after_accept"}
    returners = {"_pre_reject", "parity_reject", "_machine_refusal"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in emitters:
                for arg in node.args[1:] if name in {"_reject", "_reject_after_accept"} else node.args:
                    if name == "_reject_after_accept" and arg is node.args[0]:
                        continue
                    resolve(arg)
            if name == "Rejected":
                for kw in node.keywords:
                    if kw.arg == "reason":
                        resolve(kw.value)
        if isinstance(node, ast.FunctionDef) and node.name in returners:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    resolve(sub.value)
    return found


def test_the_catalogue_covers_every_reject_code_the_modules_can_emit():
    src = Path(__file__).resolve().parents[4] / "src" / "demo" / "execution"
    emitted: set[str] = set()
    for name in ("live.py", "parity.py", "risk_policy.py", "sizing.py"):
        emitted |= _codes_emitted_in(src / name)
    emitted = {e for e in emitted if (e and not e.startswith("execution_denied")) or e == "execution_denied"}
    assert len(emitted) > 20, emitted  # the scan really found the reason codes
    missing = sorted(code for code in emitted if G.gate_for(code) is None)
    assert missing == [], f"reject codes not in GATE_CATALOG: {missing}"


def test_size_below_min_is_the_only_sizing_reject_and_its_caps_are_catalogued():
    assert G.GATE_CATALOG["size_below_min"].gate_class is G.GateClass.SAFETY
    for cap in ("max_position_stop_risk_fraction", "max_aggregate_open_stop_risk_fraction",
                "max_cluster_stop_risk_fraction", "max_family_share_of_open_risk", "max_leverage",
                "max_portfolio_leverage", "max_margin_fraction_of_free_margin",
                "liquidation_safe_leverage", "instrument_max_leverage", "instrument_volume_max"):
        assert cap in G.CATALOG_WITH_CAPS, cap
    assert G.gate_for("execution_denied:ORDER_CHECK_10016").code == "execution_denied"


def test_funnel_groups_by_class_and_reports_temporary_limitations_separately():
    reasons = (
        ["stale_signal"] * 4 + ["size_below_min"] * 2 + ["entry_overshoot"] + ["min_space_r"] * 3
        + ["ADDON_EXPOSURE_NOT_SUPPORTED_V1"] * 2 + ["ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED"]
    )
    funnel = G.funnel(
        reasons,
        otherwise_valid=["ADDON_EXPOSURE_NOT_SUPPORTED_V1", "ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED"],
    )
    assert funnel["SAFETY"]["total"] == 6 and funnel["STRUCTURAL"]["total"] == 1
    assert funnel["LEGACY_ARBITRARY"]["total"] == 3
    temp = funnel["TEMPORARY_LIMITATION"]
    assert temp["total"] == 3 and temp["otherwise_valid_blocked"] == 2
    assert "TEMPORARY" not in funnel  # the category is reported under its own name
