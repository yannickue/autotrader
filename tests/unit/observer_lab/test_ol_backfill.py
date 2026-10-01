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


def _read(d, kind):
    return pd.concat([pd.read_parquet(d / BF.EVENT_FILES[kind]), pd.read_parquet(d / BF.CONTROL_FILES[kind])], ignore_index=True)


def _tables():
    b = built()
    d = b["mdir"]
    return b, _read(d, "table"), _read(d, "events"), _read(d, "features"), _read(d, "labels")


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
    base = pd.read_parquet(b["mdir"] / BF.CONTROL_FILES["events"])
    out2 = tempfile.mkdtemp(prefix="ol_backfill_seed_")
    try:
        with patched_generators():
            BF.run_market_backfill(b["mi"], b["mspec"], out2, seed=b["seed"], code=CODE)
            again = pd.read_parquet(f"{out2}/{MARKET}/{BF.CONTROL_FILES['events']}")
            BF.run_controls_step(b["frame"], b["mspec"], MARKET, out2, seed=b["seed"] + 1, code=CODE)
            other = pd.read_parquet(f"{out2}/{MARKET}/{BF.CONTROL_FILES['events']}")
    finally:
        shutil.rmtree(out2, ignore_errors=True)
    assert base["event_id"].tolist() == again["event_id"].tolist()
    c0, c1 = set(base.event_id), set(other.event_id)
    assert c0 != c1 or len(c0) <= 1  # a different seed draws different controls (unless there was no choice at all)


def test_controls_stay_away_from_every_generator_opportunity_not_only_the_emitted_events():
    b = built()
    opp = pd.read_parquet(b["mdir"] / "opportunity_bars.parquet")["decision_idx"].to_numpy()
    assert {100, 1600, 1610} <= set(opp.tolist())  # candidates that were filtered out of the event table (warm-up, NaN stop, wrong-side stop)
    ct = pd.read_parquet(b["mdir"] / BF.CONTROL_FILES["events"])
    assert len(ct) >= 1
    for i in ct["decision_idx"]:
        assert np.abs(opp - i).min() > 48


def test_controls_step_is_independently_rerunnable_and_never_rewrites_event_files(tmp_path):
    b = built()
    with patched_generators():
        BF.run_events_step(b["mi"], b["mspec"], tmp_path, code=CODE)
        with pytest.raises(FileNotFoundError):  # controls of a market without events are refused
            BF.run_controls_step(b["frame"], b["mspec"], "OTHER", tmp_path, code=CODE)
        c1 = BF.run_controls_step(b["frame"], b["mspec"], MARKET, tmp_path, seed=1, code=CODE)
        ev_mt = {k: (tmp_path / MARKET / f).stat().st_mtime_ns for k, f in BF.EVENT_FILES.items()}
        c2 = BF.run_controls_step(b["frame"], b["mspec"], MARKET, tmp_path, seed=2, code=CODE)  # re-run ONLY the controls with another seed
        assert c1["status_this_call"] == c2["status_this_call"] == "BUILT" and c1["fingerprint"] != c2["fingerprint"]
        assert ev_mt == {k: (tmp_path / MARKET / f).stat().st_mtime_ns for k, f in BF.EVENT_FILES.items()}  # event files untouched
        assert c2["matching_is_pre_revision"] is False and c2["matching_revision"] == BF.CONTROL_MATCHING_REVISION and c2["events_run_id"] == c1["events_run_id"]
        other_frame = make_frame(N_BARS, seed=99)
        with pytest.raises(ValueError):  # a different frame than the one the events step used
            BF.run_controls_step(other_frame, b["mspec"], MARKET, tmp_path, seed=3, code=CODE)


# ---------------------------------------------------------------------------------------------- partitions
def test_partition_column_follows_the_repo_split_with_purge_and_embargo():
    from coverage_analysis.observer_lab.splits import build_plan

    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", "2026-08-31")]
    plan = build_plan(days)
    ns = lambda s: int(pd.Timestamp(s, tz="Europe/Berlin").value)  # noqa: E731
    h = 4 * 3600 * 10**9
    dec = np.array([ns("2026-02-10 12:00"), ns(f"{plan.validation_start} 12:00"), ns("2026-07-15 12:00"), ns("2026-06-30 23:00"), ns("2026-07-01 00:30"), ns(f"{plan.validation_start} 12:00")], dtype=np.int64)
    part = BF.partitions_for(plan, dec, dec + h, 4 * 3600.0)
    assert part.tolist() == ["TRAIN", "VALIDATION", "FROZEN_OOS", "PURGED", "EMBARGO", "VALIDATION"]
    b = built()  # synthetic July-2026 frame: no TRAIN/VALIDATION days exist -> everything is FROZEN_OOS and the plan is flagged irregular
    ev = pd.read_parquet(b["mdir"] / BF.EVENT_FILES["events"])
    assert set(ev["partition"]) == {"FROZEN_OOS"} and b["manifest"]["partitions"]["regular_plan"] is False
    assert "partition" not in pd.read_parquet(b["mdir"] / BF.EVENT_FILES["features"]).columns  # a split tag (uses the label horizon), never a feature
    assert BF.load_event_table(b["mdir"])["partition"].notna().all()


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
    assert not any("labels" in p for p in opened)  # physically never opened (neither the events' nor the controls' labels file)
    assert {"decision_idx", "risk", "f_levels__role", "is_control"} <= set(df.columns)
    with pytest.raises(ValueError):
        BF.load_event_table(b["mdir"], columns=["event_id", "y_mfe_r"])
    lab = BF.load_event_table(b["mdir"], with_labels=True)
    assert "y_mfe_r" in lab.columns and len(lab) == len(df) and lab["event_id"].tolist() == df["event_id"].tolist()
    root = BF.load_event_table(b["out"])  # a root with market subdirectories
    assert len(root) == len(df)
    only_events = BF.load_event_table(b["mdir"], controls=False)
    assert not only_events["is_control"].any() and len(only_events) == len(EVENT_BARS) and len(df) > len(only_events)


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
def test_resumability_complete_steps_are_skipped_and_incomplete_ones_rebuilt(tmp_path):
    b = built()
    mdir = tmp_path / MARKET
    files = {**BF.EVENT_FILES, **{f"c_{k}": v for k, v in BF.CONTROL_FILES.items()}}
    with patched_generators():
        m1 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        mt = {k: (mdir / f).stat().st_mtime_ns for k, f in files.items()}
        m2 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        assert m1["status_this_call"] == "BUILT" and m1["controls"]["status_this_call"] == "BUILT"
        assert m2["status_this_call"] == "SKIPPED_COMPLETE" and m2["controls"]["status_this_call"] == "SKIPPED_COMPLETE" and m1["run_id"] == m2["run_id"]
        assert mt == {k: (mdir / f).stat().st_mtime_ns for k, f in files.items()}  # untouched
        (mdir / "controls_labels.parquet").unlink()  # an incomplete controls step is rebuilt alone, the events step stays
        m3 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        assert m3["status_this_call"] == "SKIPPED_COMPLETE" and m3["controls"]["status_this_call"] == "BUILT" and (mdir / "controls_labels.parquet").is_file()
        (mdir / "labels.parquet").unlink()  # an incomplete events step is rebuilt (controls depend on its fingerprint, which is unchanged)
        m4 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=3, code=CODE)
        assert m4["status_this_call"] == "BUILT" and (mdir / "labels.parquet").is_file() and m4["run_id"] == m1["run_id"]
        m5 = BF.run_market_backfill(b["mi"], b["mspec"], tmp_path, seed=4, code=CODE)  # another seed: events unchanged, controls rebuilt
        assert m5["status_this_call"] == "SKIPPED_COMPLETE" and m5["controls"]["status_this_call"] == "BUILT" and m5["controls"]["run_id"] != m1["controls"]["run_id"]
        assert not (mdir / "_parts").exists() and not (mdir / "_parts_controls").exists()
    assert pd.read_parquet(mdir / "events.parquet")["run_id"].nunique() == 1


# ---------------------------------------------------------------------------------------------- manifest
def test_manifest_content():
    b = built()
    m = json.loads((b["mdir"] / "manifest.json").read_text(encoding="utf-8"))
    c = json.loads((b["mdir"] / "controls_manifest.json").read_text(encoding="utf-8"))
    from market_observer.schema import OBSERVER_VERSION

    assert m["status"] == "COMPLETE" and m["observer_version"] == OBSERVER_VERSION and m["code"]["git_sha"] == "test" and c["seed"] == 7 and c["code"]["git_sha"] == "test"
    assert set(m["group_versions"]) == {"levels", "swings", "acceptance", "participation", "balance"} and set(m["definition_hashes"]) == set(m["group_versions"])
    assert m["dev_end_guard"]["dev_end_berlin_date"] == "2026-08-31" and m["dev_end_guard"]["guard_dev_only"] == "PASS"
    assert m["data"]["n_bars"] == N_BARS and m["data"]["first_bar_utc"] < m["data"]["last_bar_utc"] and "frame_fingerprint" in m["data"] and "missing_months_utc" in m["data"]
    assert m["events"]["n_events"] == len(EVENT_BARS) and m["events"]["by_family_variant_direction"] and m["events"]["exclusions"]["no_stop"] == 1
    assert m["events"]["n_opportunity_bars"] == len(EVENT_BARS) + 3
    assert c["match_report"]["n_events"] == len(EVENT_BARS) and "smd" in c["match_report"] and c["match_report"]["method"].startswith("observer-controls")
    assert c["matching_revision"] == BF.CONTROL_MATCHING_REVISION and not c["matching_is_pre_revision"] and "controls_in_a_different_partition_than_their_event" in c["partitions"]
    assert m["rows"]["table"] == m["rows"]["features"] == m["rows"]["labels"] == m["rows"]["events"] == m["events"]["n_events"]
    assert c["rows"]["table"] == c["rows"]["features"] == c["rows"]["labels"] == c["rows"]["events"] == c["n_controls"]
    assert m["runtime_s"] >= 0 and "peak_memory_mb" in m and m["label_convention_version"] == "obs-labels-1" and m["partitions"]["has_frozen_split"] is True
    # file layout / column naming
    schema = pq.read_schema(b["mdir"] / "table.parquet").names
    assert {"event_id", "run_id", "warmup_ok", "is_control", "control_of", "horizon_end_ts_ns", "m_warmup_ok", "partition"} <= set(schema)
    assert any(c.startswith("f_levels__") for c in schema) and any(c.startswith("v_levels") for c in schema) and any(c.startswith("y_") for c in schema)
    # the table columns are exactly ObserverRecord.to_row() (+ warmup_ok, run_id, partition): every column is one of the documented families
    assert all(c.startswith(("f_", "y_", "v_", "m_")) or c in {
        "event_id", "market", "family", "variant", "direction", "is_control", "control_of", "decision_ts_ns", "observer_version", "schema_version", "status",
        "horizon_end_ts_ns", "warmup_ok", "run_id", "partition"} for c in schema)
    assert pq.read_schema(b["mdir"] / "controls.parquet").names == schema  # controls: same columns, same versions as the events
