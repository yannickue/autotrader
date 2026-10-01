# ruff: noqa: E501
"""Observer backfill (``coverage_analysis.observer_lab.backfill``): events, stepped observer == reference, controls, labels, loader separation, guard,
resumability, manifest. Synthetic small frames (the family generator is replaced by a deterministic fake; every other component is the real one).

  event generation is deterministic + filters counted     test_event_generation_*
  stepped incremental observer == observe_event           test_stepped_observer_equals_reference_at_events_and_controls
  controls reproducible by seed, outside neighbourhoods   test_controls_*
  labels use only post-decision bars / not in features    test_labels_*
  loader cannot return labels unless asked                test_loader_*
  dev-end guard refuses later bars                        test_dev_end_guard_*
  resumability                                            test_resumability_*
  manifest content                                        test_manifest_*
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import replace

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from ol_backfill_support import (
    CODE,
    EVENT_BARS,
    MARKET,
    N_BARS,
    built,
    make_frame,
    make_inputs,
    make_mspec,
    patched_generators,
)

from alpha.common.market_data import ForwardHoldoutError
from coverage_analysis.observer_lab import backfill as BF
from coverage_analysis.observer_lab.backfill_audit import values_equal
from coverage_analysis.observer_lab.labels import EventSpec, label_event
from market_observer import observer as O


def _tables():
    b = built()
    d = b["mdir"]
    return b, pd.read_parquet(d / "table.parquet"), pd.read_parquet(d / "events.parquet"), pd.read_parquet(d / "features.parquet"), pd.read_parquet(d / "labels.parquet")


# ---------------------------------------------------------------------------------------------- events
def test_event_generation_is_deterministic_and_counts_every_exclusion():
    mi = make_inputs()
    with patched_generators():
        a, ca = BF.generate_events(mi)
        b, cb = BF.generate_events(mi)
    assert a == b and ca == cb
    assert [e.idx for e in a] == sorted(EVENT_BARS)  # sorted, one row per opportunity
    assert ca["candidates"] == len(EVENT_BARS) + 4
    assert ca["duplicate_family_variant_direction"] == 1
    assert ca["before_eval_start"] == 1
    assert ca["no_stop"] == 1 and ca.get("no_following_bar", 0) == 0
    assert ca["stop_not_on_loss_side"] == 1
    assert ca["emitted"] == len(EVENT_BARS)
    for e in a:
        assert e.family == "STRUCT" and e.variant == "breakout" and e.risk > 0 and e.entry == float(mi.data.c[e.idx])
        assert (e.entry - e.stop) * e.direction == pytest.approx(e.risk)
        assert e.structure_event_id is not None and e.structure_event_id.startswith(f"{MARKET}:{e.direction}:")


def test_event_generation_limit_keeps_the_first_events():
    mi = make_inputs()
    with patched_generators():
        rows, excl = BF.generate_events(mi, limit=3)
    assert [e.idx for e in rows] == sorted(EVENT_BARS)[:3] and excl["emitted"] == 3 and excl["emitted_before_limit"] == len(EVENT_BARS)


# ---------------------------------------------------------------------------------------------- stepped observer == reference
def test_stepped_observer_equals_reference_at_events_and_controls():
    b, table, _e, _f, _l = _tables()
    bars = BF.build_bars(b["frame"], b["mspec"], MARKET)
    cfg = BF.observer_config_for(b["mspec"])
    ts_to_idx = {int(t): k for k, t in enumerate(bars.ts_ns)}
    sample = pd.concat([table[~table.is_control].iloc[[0, 3]], table[table.is_control].iloc[[0]]])
    assert len(sample) == 3
    for row in sample.to_dict("records"):
        i = ts_to_idx[int(row["decision_ts_ns"]) - 300 * 10**9]
        ev = O.ObservedEvent(
            int(row["direction"]), float(bars.c[i]), family=row["family"], variant=row["variant"], structure_event_id=row["m_structure_event_id"] if isinstance(row["m_structure_event_id"], str) else None,
            is_control=bool(row["is_control"]), control_of=row["control_of"] if isinstance(row["control_of"], str) else None,
        )
        ref = O.observe_event(bars, i, ev, cfg).to_row()
        for k, v in ref.items():
            assert values_equal(v, row[k]), (k, v, row[k])


# ---------------------------------------------------------------------------------------------- controls
def test_controls_are_matched_marked_and_outside_every_event_neighbourhood():
    _b, table, events, _f, _l = _tables()
    ev = events[~events.is_control]
    ct = events[events.is_control]
    assert len(ev) == len(EVENT_BARS) and len(ct) >= 1
    ev_idx = ev["decision_idx"].to_numpy()
    for _, c in ct.iterrows():
        assert np.abs(ev_idx - c["decision_idx"]).min() > 48  # exclusion +-48 bars around EVERY event
        src = ev[ev.event_id == c["control_of"]]
        assert len(src) == 1 and int(src["direction"].iloc[0]) == int(c["direction"])  # direction inherited
        assert c["risk"] == pytest.approx(float(src["risk"].iloc[0])) and bool(c["is_control"])
    assert events["event_id"].is_unique and events.set_index("event_id").loc[ct["event_id"], "control_of"].isin(ev["event_id"]).all()
    assert len(table) == len(events)


def test_controls_reproducible_by_seed_and_the_seed_matters():
    b = built()
    base = pd.read_parquet(b["mdir"] / "events.parquet")
    out2 = tempfile.mkdtemp(prefix="ol_backfill_seed_")
    try:
        with patched_generators():
            BF.run_market_backfill(b["mi"], b["mspec"], out2, seed=b["seed"], code=CODE)
            again = pd.read_parquet(f"{out2}/{MARKET}/events.parquet")
            shutil.rmtree(f"{out2}/{MARKET}")
            BF.run_market_backfill(b["mi"], b["mspec"], out2, seed=b["seed"] + 1, code=CODE)
            other = pd.read_parquet(f"{out2}/{MARKET}/events.parquet")
    finally:
        shutil.rmtree(out2, ignore_errors=True)
    assert base["event_id"].tolist() == again["event_id"].tolist()
    c0 = set(base[base.is_control].event_id)
    c1 = set(other[other.is_control].event_id)
    assert c0 != c1 or len(c0) <= 1  # a different seed draws different controls (unless there was no choice at all)


# ---------------------------------------------------------------------------------------------- labels
def test_labels_use_only_post_decision_bars_and_are_not_in_the_feature_files():
    b, _t, events, feats, labels = _tables()
    bars = BF.build_bars(b["frame"], b["mspec"], MARKET)
    ts_to_idx = {int(t): k for k, t in enumerate(bars.ts_ns)}
    rng = np.random.default_rng(1)
    for row in events.iloc[[0, 2, -1]].to_dict("records"):
        i = row["decision_idx"]
        assert ts_to_idx[int(row["decision_ts_ns"]) - 300 * 10**9] == i
        # scramble EVERY bar up to and including the decision bar: a label that read any of them would change
        o, h, low, c = (np.array(x, copy=True) for x in (bars.o, bars.h, bars.l, bars.c))
        for arr in (o, h, low, c):
            arr[: i + 1] = rng.uniform(1.0, 5.0, i + 1)
        scr = replace(bars, o=o, h=h, l=low, c=c)
        spec = EventSpec(i, int(row["direction"]), float(bars.c[i]), float(row["risk"]))
        a = label_event(bars, spec)
        s = label_event(scr, spec)
        assert a.columns == s.columns and a.horizon_end_ts_ns == s.horizon_end_ts_ns
        stored = labels[labels.event_id == row["event_id"]].iloc[0]
        for k, v in a.columns.items():
            assert values_equal(v, stored[k]), (k, v, stored[k])
    assert not [c for c in feats.columns if c.startswith("y_") or c == "horizon_end_ts_ns"]
    assert not [c for c in events.columns if c.startswith("y_") or c == "horizon_end_ts_ns"]
    assert all(c.startswith("y_") or c in ("event_id", "is_control", "control_of", "horizon_end_ts_ns") for c in labels.columns)
    # labels exist for controls too
    assert labels[labels.is_control].shape[0] >= 1 and labels["y_fav050_before_adv050"].notna().any()


# ---------------------------------------------------------------------------------------------- loader
def test_loader_cannot_return_labels_unless_asked(monkeypatch):
    b = built()
    opened: list[str] = []
    real = pd.read_parquet

    def spy(path, *a, **k):
        opened.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(pd, "read_parquet", spy)
    df = BF.load_event_table(b["mdir"])
    assert not any(c.startswith("y_") or c == "horizon_end_ts_ns" for c in df.columns)
    assert not any(p.endswith("labels.parquet") for p in opened)  # physically never opened
    assert {"decision_idx", "risk", "f_levels__role", "is_control"} <= set(df.columns)
    with pytest.raises(ValueError):
        BF.load_event_table(b["mdir"], columns=["event_id", "y_mfe_r"])
    lab = BF.load_event_table(b["mdir"], with_labels=True)
    assert "y_mfe_r" in lab.columns and len(lab) == len(df) and lab["event_id"].tolist() == df["event_id"].tolist()
    root = BF.load_event_table(b["out"])  # a root with market subdirectories
    assert len(root) == len(df)


def test_loader_refuses_a_directory_whose_feature_file_leaks_a_label(tmp_path):
    b = built()
    d = tmp_path / MARKET
    shutil.copytree(b["mdir"], d)
    f = pd.read_parquet(d / "features.parquet")
    f["y_mfe_r"] = 1.0
    f.to_parquet(d / "features.parquet")
    with pytest.raises(RuntimeError):
        BF.load_event_table(d)


# ---------------------------------------------------------------------------------------------- guard
def test_dev_end_guard_refuses_later_bars(tmp_path):
    late = make_frame(60, pd.Timestamp("2026-09-02 08:00", tz="UTC"))
    mi = make_inputs(late)
    with pytest.raises(ForwardHoldoutError), patched_generators():
        BF.run_market_backfill(mi, make_mspec(), tmp_path, code=CODE)
    assert not (tmp_path / MARKET / "manifest.json").exists()
    # one bar after the Berlin dev-end date inside an otherwise valid frame is refused as well
    edge = make_frame(40, pd.Timestamp("2026-08-31 21:40", tz="UTC"))  # Berlin 23:40 on 08-31 ... 00:00+ on 09-01
    with pytest.raises(ForwardHoldoutError), patched_generators():
        BF.run_market_backfill(make_inputs(edge), make_mspec(), tmp_path, code=CODE)


# ---------------------------------------------------------------------------------------------- resumability
def test_resumability_complete_runs_are_skipped_and_incomplete_ones_rebuilt(tmp_path):
    b = built()
    with patched_generators():
        m1 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        mt = {k: (tmp_path / MARKET / f"{k}.parquet").stat().st_mtime_ns for k in BF.FILES}
        m2 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        assert m1["status_this_call"] == "BUILT" and m2["status_this_call"] == "SKIPPED_COMPLETE" and m1["run_id"] == m2["run_id"]
        assert mt == {k: (tmp_path / MARKET / f"{k}.parquet").stat().st_mtime_ns for k in BF.FILES}  # untouched
        (tmp_path / MARKET / "labels.parquet").unlink()  # an incomplete directory is rebuilt, not trusted
        m3 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        assert m3["status_this_call"] == "BUILT" and (tmp_path / MARKET / "labels.parquet").is_file()
        m4 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=4, code=CODE)  # another seed = another fingerprint = rebuilt
        assert m4["status_this_call"] == "BUILT" and m4["run_id"] != m1["run_id"]
        assert not (tmp_path / MARKET / "_parts").exists()
    assert pd.read_parquet(tmp_path / MARKET / "events.parquet")["run_id"].nunique() == 1


# ---------------------------------------------------------------------------------------------- manifest
def test_manifest_content():
    b = built()
    m = json.loads((b["mdir"] / "manifest.json").read_text(encoding="utf-8"))
    from market_observer.schema import OBSERVER_VERSION

    assert m["status"] == "COMPLETE" and m["observer_version"] == OBSERVER_VERSION and m["seed"] == 7 and m["code"]["git_sha"] == "test"
    assert set(m["group_versions"]) == {"levels", "swings", "acceptance", "participation", "balance"} and set(m["definition_hashes"]) == set(m["group_versions"])
    assert m["dev_end_guard"]["dev_end_berlin_date"] == "2026-08-31" and m["dev_end_guard"]["guard_dev_only"] == "PASS"
    assert m["data"]["n_bars"] == N_BARS and m["data"]["first_bar_utc"] < m["data"]["last_bar_utc"] and "frame_fingerprint" in m["data"] and "missing_months_utc" in m["data"]
    assert m["events"]["n_events"] == len(EVENT_BARS) and m["events"]["by_family_variant_direction"] and m["events"]["exclusions"]["no_stop"] == 1
    assert m["match_report"]["n_events"] == len(EVENT_BARS) and "smd" in m["match_report"] and m["match_report"]["method"].startswith("observer-controls")
    assert m["rows"]["table"] == m["rows"]["features"] == m["rows"]["labels"] == m["rows"]["events"] == m["events"]["n_events"] + m["events"]["n_controls"]
    assert m["runtime_s"] >= 0 and "peak_memory_mb" in m and m["label_convention_version"] == "obs-labels-1"
    # file layout / column naming
    schema = pq.read_schema(b["mdir"] / "table.parquet").names
    assert {"event_id", "run_id", "warmup_ok", "is_control", "control_of", "horizon_end_ts_ns", "m_warmup_ok"} <= set(schema)
    assert any(c.startswith("f_levels__") for c in schema) and any(c.startswith("v_levels") for c in schema) and any(c.startswith("y_") for c in schema)
    # the table columns are exactly ObserverRecord.to_row() (+ warmup_ok, run_id): every column is one of the documented families
    assert all(c.startswith(("f_", "y_", "v_", "m_")) or c in {
        "event_id", "market", "family", "variant", "direction", "is_control", "control_of", "decision_ts_ns", "observer_version", "schema_version", "status",
        "horizon_end_ts_ns", "warmup_ok", "run_id"} for c in schema)
