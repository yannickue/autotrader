import pyarrow.parquet as pq
import pytest
from demo_factories import drive_full_trade, make_decision, make_label, make_snapshot

from demo.export import export_all, flatten, rows_to_table, write_parquet_atomic
from demo.store import DemoStore


@pytest.fixture
def store(tmp_path):
    s = DemoStore(tmp_path / "d.db")
    yield s
    s.close()


def _fill(store):
    drive_full_trade(store, make_snapshot(i=0, phase="DISCOVERY"), net_r=1.5)
    drive_full_trade(store, make_snapshot(i=1, phase="FROZEN"), net_r=-1.0)
    rej = make_snapshot(i=2, phase="DISCOVERY")
    store.record_snapshot(rej)
    store.record_decision(make_decision(rej, accepted=False))
    store.record_counterfactual(make_label(rej, r=-1.0))
    return rej


def test_flatten():
    f = flatten({"a": {"b": 1, "c": {"d": [1, 2]}}, "e": {}, "g": None})
    assert f == {"a_b": 1, "a_c_d": "[1, 2]", "e": None, "g": None}


def test_export_partitions_by_phase(store, tmp_path):
    rej = _fill(store)
    out = tmp_path / "pq"
    written = export_all(store, out)
    assert (out / "opportunities" / "phase=DISCOVERY" / "data.parquet").exists()
    assert (out / "opportunities" / "phase=FROZEN" / "data.parquet").exists()
    assert (out / "trades" / "phase=FROZEN" / "data.parquet").exists()
    assert not (out / "counterfactuals" / "phase=FROZEN").exists()
    assert len(written["opportunities"]) == 2
    opp = pq.read_table(out / "opportunities" / "phase=DISCOVERY" / "data.parquet").to_pylist()
    assert len(opp) == 2 and {r["phase"] for r in opp} == {"DISCOVERY"}
    r_rej = next(r for r in opp if r["opportunity_id"] == rej.opportunity_id)
    assert r_rej["decision_accepted"] is False and r_rej["cf_hypothetical_r"] == -1.0
    assert (
        r_rej["geometry_risk_distance"] == 1.5 and r_rej["market_state_clock_local_minute"] == 600
    )
    tr = pq.read_table(out / "trades" / "phase=DISCOVERY" / "data.parquet").to_pylist()
    assert len(tr) == 1 and tr[0]["outcome_net_r"] == 1.5 and tr[0]["execution_fees"] == -1.0
    cf = pq.read_table(out / "counterfactuals" / "phase=DISCOVERY" / "data.parquet").to_pylist()
    assert cf[0]["hypothetical_r"] == -1.0


def test_export_single_phase_and_idempotent_no_tmp_left(store, tmp_path):
    _fill(store)
    out = tmp_path / "pq"
    export_all(store, out, phase="FROZEN")
    assert not (out / "opportunities" / "phase=DISCOVERY").exists()
    export_all(store, out, phase="FROZEN")
    assert not list(out.rglob("*.tmp-*"))


def test_mixed_types_fall_back_to_json_strings():
    t = rows_to_table([{"x": 1}, {"x": "a"}, {"y": 2.5}])
    assert t.column("x").to_pylist() == ["1", '"a"', None]
    assert t.column("y").to_pylist() == [None, None, 2.5]


def test_atomic_write_cleans_tmp_on_failure(tmp_path):
    class Boom:
        pass

    with pytest.raises(Exception):  # noqa: B017
        write_parquet_atomic(Boom(), tmp_path / "x" / "data.parquet")  # type: ignore[arg-type]
    assert not list(tmp_path.rglob("*.tmp-*")) and not (tmp_path / "x" / "data.parquet").exists()
