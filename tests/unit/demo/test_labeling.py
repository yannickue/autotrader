from datetime import timedelta

import pytest
from demo_factories import T0, iso, make_decision, make_snapshot

from demo.labeling import (
    Bar,
    Fill,
    PathPoint,
    label_counterfactuals,
    outcome_from_fills,
    simulate_hypothetical,
)
from demo.store import DemoStore


def bar(k, o, h, lo, c, spread=0.0, base=T0):
    return Bar(iso(base + timedelta(minutes=5 * k)), o, h, lo, c, spread)


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    s.close()


def _reject(store, **kw):
    snap = make_snapshot(**kw)
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=False))
    return snap


def now_after(snap, extra=60):
    return iso(T0 + timedelta(seconds=snap.geometry.expected_horizon_s + extra))


def test_long_target_hit(store):
    snap = _reject(store)
    bars = [bar(0, 100, 101, 99.5, 100.5), bar(1, 100.5, 103.2, 100.2, 103)]
    (lab,) = label_counterfactuals(store, lambda *_: bars, now_after(snap))
    assert lab.hypothetical_r == pytest.approx(2.0)
    assert lab.target_before_stop is True
    assert lab.hypothetical_mfe_r == pytest.approx(2.0)
    assert lab.hypothetical_mae_r == pytest.approx(0.5 / 1.5)
    assert store.get_counterfactual(snap.opportunity_id) == lab


def test_stop_first_when_bar_touches_both(store):
    snap = _reject(store)
    bars = [bar(0, 100, 103.5, 98.0, 101)]  # low<=stop AND high>=target: pessimistic -> stop
    (lab,) = label_counterfactuals(store, lambda *_: bars, now_after(snap))
    assert lab.hypothetical_r == pytest.approx(-1.0)
    assert lab.target_before_stop is False
    assert lab.hypothetical_mfe_r == 0.0  # no MFE credit on the stopping bar (sim semantics)
    assert lab.hypothetical_mae_r == pytest.approx(2.0 / 1.5)


def test_gap_through_stop_exits_at_open():
    r = simulate_hypothetical(
        direction=1, entry=100, stop=98.5, target=103, bars=[bar(0, 97.5, 98, 97, 97.8)]
    )
    assert r.exit_kind == "STOP_GAP" and r.r == pytest.approx(-2.5 / 1.5)


def test_short_uses_ask_side_and_spread():
    # short entry 100 stop 101.5 target 97; ask = bid + 0.2
    r = simulate_hypothetical(
        direction=-1,
        entry=100,
        stop=101.5,
        target=97,
        bars=[bar(0, 100, 101.4, 99, 100, spread=0.2)],
    )
    assert r.exit_kind == "STOP" and r.r == pytest.approx(-1.0)  # 101.4+0.2 >= 101.5
    r2 = simulate_hypothetical(
        direction=-1,
        entry=100,
        stop=101.5,
        target=97,
        bars=[bar(0, 100, 100.5, 96.7, 97.5, spread=0.2)],
    )
    assert r2.exit_kind == "TARGET" and r2.r == pytest.approx(2.0)


def test_horizon_without_hit_marks_to_last_close(store):
    snap = _reject(store)
    bars = [bar(k, 100, 100.6, 99.6, 100.3) for k in range(12)]
    (lab,) = label_counterfactuals(store, lambda *_: bars, now_after(snap))
    assert lab.target_before_stop is None
    assert lab.hypothetical_r == pytest.approx(0.3 / 1.5)


def test_not_labelled_before_horizon_or_when_uncovered(store):
    snap = _reject(store)
    full = [bar(k, 100, 100.6, 99.6, 100.3) for k in range(12)]
    early = iso(T0 + timedelta(seconds=snap.geometry.expected_horizon_s - 1))
    assert label_counterfactuals(store, lambda *_: full, early) == []
    # bars stop half-way: wait for grace period
    partial = full[:5]
    assert label_counterfactuals(store, lambda *_: partial, now_after(snap)) == []
    late = iso(T0 + timedelta(seconds=snap.geometry.expected_horizon_s + 7 * 3600))
    assert len(label_counterfactuals(store, lambda *_: partial, late)) == 1
    assert label_counterfactuals(store, lambda *_: partial, late) == []  # once only


def test_accepted_decisions_never_labelled(store):
    snap = make_snapshot()
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, accepted=True))
    assert (
        label_counterfactuals(store, lambda *_: [bar(0, 100, 101, 99, 100)], now_after(snap)) == []
    )


def test_leakage_only_bars_after_signal_are_used(store):
    snap = _reject(store)
    calls = []
    pre = Bar(iso(T0 - timedelta(minutes=5)), 100, 200.0, 0.0, 100)  # would blow up target AND stop
    post_end = Bar(iso(T0 + timedelta(hours=1)), 100, 200.0, 0.0, 100)  # at/after horizon end
    good = [bar(k, 100, 100.6, 99.6, 100.3) for k in range(12)]

    def provider(market, start, end):
        calls.append((market, start, end))
        return [pre, *good, post_end]

    (lab,) = label_counterfactuals(store, provider, now_after(snap))
    assert lab.hypothetical_r == pytest.approx(0.3 / 1.5) and lab.target_before_stop is None
    assert calls == [("GER40", snap.signal_ts_utc, lab.horizon_end_utc)]


def test_label_does_not_change_predecision_records(store):
    snap = _reject(store)
    dec_before = store.get_decision(snap.opportunity_id)
    raw_before = store._conn.execute("SELECT json FROM snapshots").fetchone()[0]
    label_counterfactuals(store, lambda *_: [bar(0, 100, 103.5, 99.8, 103)], now_after(snap))
    assert store._conn.execute("SELECT json FROM snapshots").fetchone()[0] == raw_before
    assert store.get_decision(snap.opportunity_id) == dec_before
    assert store.get_snapshot(snap.opportunity_id) == snap


def test_label_independent_of_future_bar_content(store, tmp_path):
    """Changing bars strictly after the horizon must not change the label."""
    snap = _reject(store)
    good = [bar(k, 100, 100.6, 99.6, 100.3) for k in range(12)]
    other = DemoStore(tmp_path / "o.db")
    other.record_snapshot(snap)
    other.record_decision(make_decision(snap, accepted=False))
    future = [bar(k, 50, 500, 1, 60) for k in range(12, 30)]
    (a,) = label_counterfactuals(store, lambda *_: good, now_after(snap))
    (b,) = label_counterfactuals(other, lambda *_: good + future, now_after(snap))
    assert (a.hypothetical_r, a.hypothetical_mfe_r, a.hypothetical_mae_r) == (
        b.hypothetical_r,
        b.hypothetical_mfe_r,
        b.hypothetical_mae_r,
    )
    other.close()


# ---- outcome_from_fills ---------------------------------------------------------------------
def test_outcome_r_from_actual_fill_not_intended_entry():
    # intended entry 100 (stop 98.5), but filled 100.5 -> risk distance is 2.0, not 1.5
    e = Fill(100.5, 2.0, iso(T0))
    x = Fill(103.5, 2.0, iso(T0 + timedelta(minutes=30)))
    out = outcome_from_fills(
        direction=1,
        entry_fill=e,
        initial_stop=98.5,
        exit_fills=[x],
        exit_reason="TARGET",
        value_per_unit_eur=1.0,
        fees_eur=-2.0,
        swap_eur=-1.0,
        path=[
            PathPoint(iso(T0 + timedelta(minutes=10)), 104.0, 100.0),
            PathPoint(iso(T0 + timedelta(minutes=20)), 102.0, 99.5),
        ],
    )
    assert out.gross_r == pytest.approx(1.5)  # 3.0 / 2.0
    assert out.pnl_eur == pytest.approx(3.0 * 2.0 - 3.0)
    assert out.net_r == pytest.approx(1.5 - 3.0 / (2.0 * 2.0))
    assert out.mfe_r == pytest.approx(3.5 / 2.0) and out.time_to_mfe_s == 600
    assert out.mae_r == pytest.approx(1.0 / 2.0) and out.time_to_mae_s == 1200
    assert out.holding_s == 1800 and out.exit_reason == "TARGET"
    assert out.closed_utc.startswith("2026-10-01T08:30")


def test_outcome_short_and_partials_and_path_window():
    e = Fill(100.0, 2.0, iso(T0))
    xs = [
        Fill(98.0, 1.0, iso(T0 + timedelta(minutes=10))),
        Fill(97.0, 1.0, iso(T0 + timedelta(minutes=20))),
    ]
    out = outcome_from_fills(
        direction=-1,
        entry_fill=e,
        initial_stop=101.0,
        exit_fills=xs,
        exit_reason="SESSION_END",
        value_per_unit_eur=10.0,
        path=[
            PathPoint(iso(T0 - timedelta(minutes=5)), 500.0, 1.0),  # before entry: ignored
            PathPoint(iso(T0 + timedelta(hours=2)), 500.0, 1.0),
        ],
    )  # after exit: ignored
    assert out.gross_r == pytest.approx(2.5) and out.partial_fills == 1
    assert out.net_r == pytest.approx(2.5)
    assert out.mfe_r == pytest.approx(3.0) and out.mae_r == 0.0 and out.time_to_mae_s is None


def test_outcome_validation():
    e = Fill(100.0, 1.0, iso(T0))
    x = Fill(101.0, 1.0, iso(T0 + timedelta(minutes=1)))
    with pytest.raises(ValueError):
        outcome_from_fills(
            direction=1,
            entry_fill=e,
            initial_stop=101.0,
            exit_fills=[x],
            exit_reason="STOP",
            value_per_unit_eur=1.0,
        )
    with pytest.raises(ValueError):
        outcome_from_fills(
            direction=1,
            entry_fill=e,
            initial_stop=99.0,
            exit_fills=[],
            exit_reason="STOP",
            value_per_unit_eur=1.0,
        )
