import json
from datetime import timedelta

import pytest
from demo_factories import (
    T0,
    drive_full_trade,
    make_decision,
    make_label,
    make_snapshot,
)

from demo.report import (
    MILESTONES,
    build_report,
    max_drawdown_r,
    pending_milestone,
    render_markdown,
    should_emit_milestone,
    write_report,
)
from demo.store import DemoStore


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    s.close()


def _trades(store, rs, phase="DISCOVERY", market="GER40"):
    for i, r in enumerate(rs):
        snap = make_snapshot(
            i=i + len(store.list_snapshots()),
            phase=phase,
            market=market,
            direction=1 if i % 2 == 0 else -1,
        )
        drive_full_trade(store, snap, net_r=r, closed=T0 + timedelta(hours=i + 1))


def test_empty_report(store):
    rep = build_report(store)
    assert rep["metrics"]["trades"] == 0 and rep["metrics"]["expected_r"] is None
    assert "nothing can be concluded" in rep["sample_size_statement"]
    render_markdown(rep)


def test_core_metrics(store):
    _trades(store, [2.0, -1.0, 1.0, -1.0, 3.0])
    m = build_report(store)["metrics"]
    assert m["trades"] == 5 and m["wins"] == 3 and m["losses"] == 2
    assert m["winrate"] == pytest.approx(0.6)
    assert m["avg_win_r"] == pytest.approx(2.0) and m["avg_loss_r"] == pytest.approx(-1.0)
    assert m["expected_r"] == pytest.approx(0.8)
    assert m["profit_factor"] == pytest.approx(6.0 / 2.0)
    assert m["cumulative_r"] == pytest.approx(4.0)
    assert m["pnl_eur"] == pytest.approx(400.0)
    assert m["costs_eur"] == pytest.approx(5 * 1.0)  # fees -1.0 each -> cost +1.0
    assert m["max_drawdown_r"] == pytest.approx(1.0)
    assert m["active_days"] == 1 and m["trades_per_day"] == pytest.approx(5.0)
    assert m["avg_slippage"] == pytest.approx(0.05)


def test_no_loss_profit_factor_undefined(store):
    _trades(store, [1.0, 2.0])
    m = build_report(store)["metrics"]
    assert m["profit_factor"] is None and "undefined" in m["profit_factor_note"]


def test_max_drawdown():
    assert max_drawdown_r([1, -2, -1, 4, -1]) == pytest.approx(3.0)
    assert max_drawdown_r([]) == 0.0 and max_drawdown_r([1, 1]) == 0.0


def test_all_trades_listed_and_no_profitability_claim(store):
    _trades(store, [1.0] * 12)
    rep = build_report(store)
    assert len(rep["trades"]) == 12
    st = rep["sample_size_statement"]
    assert "too small" in st and "no claim of profitability" in st and "not a clean holdout" in st
    md = render_markdown(rep)
    assert "All trades (12)" in md and "profitable" not in md.lower().replace("profitability", "")


def test_phase_filter_separates_discovery_and_frozen(store):
    _trades(store, [1.0, 1.0], phase="DISCOVERY")
    _trades(store, [-1.0], phase="FROZEN")
    assert build_report(store, "DISCOVERY")["metrics"]["trades"] == 2
    f = build_report(store, "FROZEN")
    assert f["metrics"]["trades"] == 1 and "not a clean holdout" not in f["sample_size_statement"]
    assert build_report(store)["metrics"]["trades"] == 3
    with pytest.raises(ValueError):
        build_report(store, "LIVE")


def test_breakdowns(store):
    _trades(store, [1.0, -1.0], market="GER40")
    _trades(store, [2.0], market="XAUUSD")
    b = build_report(store)["breakdowns"]
    assert b["market"]["GER40"]["n"] == 2 and b["market"]["XAUUSD"]["n"] == 1
    assert b["market"]["XAUUSD"]["low_n"] is True
    assert set(b["direction"]) == {"long", "short"}
    assert b["family"]["orb"]["n"] == 3
    assert b["hour_local"]["10"]["n"] == 3  # local_minute 600
    assert b["confluence"]["2"]["n"] == 3
    assert sum(g["n"] for g in b["spread_bucket"].values()) == 3
    assert sum(g["n"] for g in b["quality"].values()) == 3


def test_accepted_vs_rejected_uses_counterfactuals(store):
    _trades(store, [1.0])
    for i, r in enumerate([-1.0, 2.0, 0.5]):
        s = make_snapshot(i=100 + i)
        store.record_snapshot(s)
        store.record_decision(make_decision(s, accepted=False))
        if i < 2:
            store.record_counterfactual(make_label(s, r=r))
    avr = build_report(store)["accepted_vs_rejected"]
    assert avr["accepted"]["expected_net_r"] == pytest.approx(1.0)
    assert avr["rejected"]["decisions"] == 3 and avr["rejected"]["labelled"] == 2
    assert avr["rejected"]["unlabelled"] == 1
    assert avr["rejected"]["expected_hypothetical_r"] == pytest.approx(0.5)


def test_milestones_persisted_no_duplicates(tmp_path):
    p = tmp_path / "d.db"
    s = DemoStore(p)
    assert should_emit_milestone(s, 9) is None
    assert should_emit_milestone(s, 10) == 10
    assert should_emit_milestone(s, 10) is None
    assert should_emit_milestone(s, 24) is None
    s.close()
    s = DemoStore(p)  # persisted across restart
    assert should_emit_milestone(s, 10) is None
    assert should_emit_milestone(s, 26) == 25
    assert should_emit_milestone(s, 26) is None
    assert should_emit_milestone(s, 600) == 500  # jumps: highest only, lower ones superseded
    assert pending_milestone(s, 600) is None
    # phases are tracked independently
    assert should_emit_milestone(s, 10, phase="FROZEN") == 10
    assert MILESTONES == (10, 25, 50, 100, 250, 500)
    s.close()


def test_write_report_files(store, tmp_path):
    _trades(store, [1.0, -1.0])
    md, js = write_report(store, tmp_path / "out", "DISCOVERY", tag="m10")
    assert md.name == "report-DISCOVERY-m10.md" and md.read_text(encoding="utf-8").startswith(
        "# DEMO report"
    )
    assert json.loads(js.read_text(encoding="utf-8"))["metrics"]["trades"] == 2
    assert not list((tmp_path / "out").glob("*.tmp-*"))
