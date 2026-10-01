# ruff: noqa: E501
"""Gate C script (scripts/observer_gate_c.py) on SYNTHETIC mini backfills only: no real backfill file is opened by any test here.

Covers: preregistration parsing, an end-to-end fit -> validate -> oos chain, parallel == serial (bit-identical), registry separation / no best-of-N,
dry-run reads no data, forward period refused, stop rules, negative control (random feature must not show enrichment even when events differ from
controls overall), read-only backfill directory.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS = str(Path(__file__).resolve().parents[3] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import observer_gate_c as G  # noqa: E402

from alpha.common.market_data import ForwardHoldoutError  # noqa: E402
from coverage_analysis.observer_lab import stats as ST  # noqa: E402

DAY_NS = 86_400 * 10**9
T0 = int(pd.Timestamp("2026-01-05 10:00", tz="Europe/Berlin").value)
SPLIT = (("TRAIN", 0, 90), ("VALIDATION", 90, 130), ("FROZEN_OOS", 130, 170))
PER_DAY = 12
LABEL = "y_fav050_before_adv050"
PLANT_THRESHOLD = 0.43  # ~ 2/3 quantile of N(0,1): the top tercile of the planted feature


def raw_feature_columns(prereg: G.Prereg) -> list[str]:
    cols: list[str] = []
    for h in (*prereg.spec["hypotheses"], *prereg.spec["negative_controls"]):
        cols.extend(G.NEEDS.get(h["feature"], (h["feature"],)))
    return list(dict.fromkeys(cols))


def make_market(mdir: Path, prereg: G.Prereg, seed: int, planted: float, extra_ts_ns: int | None = None) -> None:
    rng = np.random.default_rng(seed)
    cols = raw_feature_columns(prereg)
    ev_rows, ct_rows = [], []
    for part, d0, d1 in SPLIT:
        for d in range(d0, d1):
            for k in range(PER_DAY):
                eid = f"e{d}_{k}"
                ts = T0 + d * DAY_NS + k * 300 * 10**9
                feats = {}
                for c in cols:
                    if c == "f_swings__ema_trend":
                        feats[c] = str(rng.choice(["up", "down", "flat"]))
                    elif c == "f_swings__m15_sequence":
                        feats[c] = str(rng.choice(["UP_SEQUENCE", "DOWN_SEQUENCE", "MIXED_TRANSITION", "RANGE_OR_UNDEFINED"]))
                    elif c == "f_acceptance__reclaim_occurred":
                        feats[c] = bool(rng.random() < 0.4)
                    elif c == "f_acceptance__time_held_beyond_level_bars":
                        feats[c] = int(rng.integers(0, 60))
                    else:
                        feats[c] = float(rng.normal())
                p = 0.5 + (planted if feats["f_acceptance__followthrough_dir_atr"] > PLANT_THRESHOLD else 0.0)
                y = float(rng.random() < p)
                base = {"direction": int(rng.choice([-1, 1])), "warmup_ok": True, "partition": part}
                ev_rows.append({"event_id": eid, "is_control": False, "control_of": None, "decision_ts_ns": ts, **base, LABEL: np.nan if rng.random() < 0.05 else y, **feats})
                cf = {}
                for c in cols:  # controls: same marginal distributions, independent values
                    if c == "f_swings__ema_trend":
                        cf[c] = str(rng.choice(["up", "down", "flat"]))
                    elif c == "f_swings__m15_sequence":
                        cf[c] = str(rng.choice(["UP_SEQUENCE", "DOWN_SEQUENCE", "MIXED_TRANSITION", "RANGE_OR_UNDEFINED"]))
                    elif c == "f_acceptance__reclaim_occurred":
                        cf[c] = bool(rng.random() < 0.4)
                    elif c == "f_acceptance__time_held_beyond_level_bars":
                        cf[c] = int(rng.integers(0, 60))
                    else:
                        cf[c] = float(rng.normal())
                ct_rows.append({"event_id": f"c_{eid}", "is_control": True, "control_of": eid, "decision_ts_ns": ts + 7 * 300 * 10**9, **{**base, "direction": int(rng.choice([-1, 1]))}, LABEL: float(rng.random() < 0.5), **cf})
    for i in range(3):  # tags the lab drops and counts
        ev_rows.append({"event_id": f"p{i}", "is_control": False, "control_of": None, "decision_ts_ns": T0 + 89 * DAY_NS + 1000 * (i + 1), "direction": 1, "warmup_ok": True, "partition": "PURGED", LABEL: 1.0, **{c: v for c, v in ev_rows[0].items() if c in cols}})
    if extra_ts_ns is not None:
        ev_rows.append({**ev_rows[0], "event_id": "fwd0", "decision_ts_ns": extra_ts_ns, "partition": "FORWARD"})
    mdir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ev_rows).to_parquet(mdir / "table.parquet", index=False)
    pd.DataFrame(ct_rows).to_parquet(mdir / "controls.parquet", index=False)


@pytest.fixture(scope="module")
def prereg() -> G.Prereg:
    return G.load_prereg(G.DEFAULT_PREREG)


def build_root(tmp_path: Path, prereg: G.Prereg, markets=("GER40", "NAS100"), planted=0.3, seed=1) -> Path:
    root = tmp_path / "backfill"
    for i, m in enumerate(markets):
        make_market(root / m, prereg, seed + i, planted)
    return root


def run(root: Path, out: Path, stage: str = "fit", jobs: int = 1, markets=("GER40", "NAS100"), extra: tuple[str, ...] = ()) -> int:
    return G.main(["--root", str(root), "--out", str(out), "--stage", stage, "--jobs", str(jobs), "--markets", *markets, *extra])


def stage_of(out: Path, stage: str) -> dict:
    return json.loads((out / "observer_gate_c_report.json").read_text(encoding="utf-8"))["stages"][stage]


def tree_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def planted_run(tmp_path_factory, prereg):
    tmp = tmp_path_factory.mktemp("gc_planted")
    root = build_root(tmp, prereg, planted=0.3)
    before = tree_hash(root)
    out = tmp / "out_serial"
    assert run(root, out, "fit", jobs=1) == 0
    return {"root": root, "out": out, "tmp": tmp, "before": before}


# ---------------------------------------------------------------------------------------------- preregistration
def test_preregistration_parses_and_defines_the_family(prereg):
    s = prereg.spec
    assert s["prereg_version"] == "observer-gate-c-prereg-1" and s["label"] == "y_fav050_before_adv050" and len(s["hypotheses"]) == 12
    assert set(s["stages"]) == {"fit", "validate", "oos"} and "forward" not in json.dumps(s["stages"]).lower()
    assert not any("fib" in h["feature"].lower() for h in (*s["hypotheses"], *s["negative_controls"])), "Fibonacci is a separate placebo lane, not part of this family"
    assert prereg.file_sha256 and prereg.json_sha256 and G.market_scope(prereg, "GER40") == "core" and G.market_scope(prereg, "BTCUSD") == "explore"
    plan = G.build_plan(prereg, "fit", list(s["markets"]["core"]), None)
    fams = G.family_sizes(plan)
    assert len(plan) == (12 + 2) * 5 and fams["gatec|fit|core|y_fav050_before_adv050|acceptance"] == 20 and fams["gatec|fit|core|y_fav050_before_adv050|ctrl"] == 10
    assert G.choose_B(prereg, fams).B == 8000 and not ST.is_power_limited(20, 8000)  # B >= 20 m / alpha for the largest family


def test_malformed_preregistration_is_refused(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("no block here", encoding="utf-8")
    with pytest.raises(ValueError):
        G.load_prereg(p)
    spec = json.loads(json.dumps(G.load_prereg(G.DEFAULT_PREREG).spec))
    spec["hypotheses"][0]["feature"] = "f_fib__level"  # group / column mismatch
    p.write_text(f"{G.MARKER_BEGIN}\n```json\n{json.dumps(spec)}\n```\n{G.MARKER_END}", encoding="utf-8")
    with pytest.raises(ValueError):
        G.load_prereg(p)


# ---------------------------------------------------------------------------------------------- end to end on synthetic data
def test_fit_finds_the_planted_cell_and_the_negative_control_stays_quiet(planted_run):
    st = stage_of(planted_run["out"], "fit")
    assert st["status"] == "COMPLETE" and st["verdict"] == G.V_FIT_OK and st["negative_control_failures"] == []
    assert {"GER40|H01", "NAS100|H01"} <= set(st["survivors"])
    res = {r["key"] + "|" + r["id"]: r for r in st["results"]}
    h01 = res["GER40|H01|H01"]
    assert h01["status"] == ST.SIGNIFICANT_ADJUSTED and h01["sign_check"] == "EXPECTED" and h01["delta"] > 0.1 and h01["m_family"] == 8 and h01["B"] == st["B"] >= 3200
    # events differ from controls overall (the planted effect), yet the information-free random feature shows no cell enrichment: the difference-to-base design works
    assert h01["base_delta"] > 0.05
    nc = [r for r in st["results"] if r["kind"] == "nc"]
    assert len(nc) == 4 and all(r["status"] in (ST.NOT_SIGNIFICANT, ST.INSUFFICIENT_EVIDENCE) and not r["survivor"] for r in nc)
    assert (planted_run["out"] / "observer_gate_c_report.md").read_text(encoding="utf-8").count("GER40") > 5
    assert st["markets"]["GER40"]["counts"]["n_purged_embargo_rows"] == 3 and st["markets"]["GER40"]["cell_defs"]["f_acceptance__followthrough_dir_atr"]["kind"] == "quantile"


def test_unplanted_data_gives_no_enrichment_and_the_stop_rule_blocks_the_next_stage(tmp_path, prereg):
    root = build_root(tmp_path, prereg, planted=0.0, seed=50)
    out = tmp_path / "out"
    assert run(root, out, "fit") == 0
    st = stage_of(out, "fit")
    assert st["verdict"] in (G.V_NONE, G.V_FIT_OK) and st["negative_control_failures"] == []
    if st["verdict"] == G.V_NONE:  # (a nominal 5 % false-positive chance exists per family; with this fixed seed the null stays null)
        assert st["survivors"] == []
        assert run(root, out, "validate") == 3
        assert "validate" not in json.loads((out / "observer_gate_c_report.json").read_text(encoding="utf-8"))["stages"]


def test_parallel_equals_serial_bit_identical(planted_run, prereg):
    out2 = planted_run["tmp"] / "out_parallel"
    assert run(planted_run["root"], out2, "fit", jobs=2) == 0
    a, b = stage_of(planted_run["out"], "fit"), stage_of(out2, "fit")
    for k in ("results", "markets", "families", "survivors", "verdict", "B", "n_hypotheses_ever"):
        assert a[k] == b[k], k
    ra = json.loads((planted_run["out"] / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    rb = json.loads((out2 / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert {h: v["p"] for h, v in ra["hypotheses"].items()} == {h: v["p"] for h, v in rb["hypotheses"].items()}
    assert b["timing"]["jobs"] == 2 and sorted(b["timing"]["evaluated_markets"]) == ["GER40", "NAS100"]


def test_registry_is_separate_persistent_and_forbids_best_of_n(planted_run, tmp_path):
    out, root = planted_run["out"], planted_run["root"]
    reg = json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert reg["name"] == "observer_gate_c" and reg["n_hypotheses_ever"] == 28 and all(h.startswith("gatec|fit|") for h in reg["hypotheses"])
    assert len(reg["families"]) == 6 and all(v["n_results_recorded_at_declaration"] == 0 for v in reg["families"].values())  # declared before any result existed
    with pytest.raises(ValueError):  # a registry of another name (e.g. the lab's / Gate B's) can never be mixed in
        ST.HypothesisRegistry("some_other_registry", out / G.REGISTRY_FILE)
    # an identical re-run (cache hit) reproduces the registered results; a forced recompute of identical data is bit-identical, so it is accepted too
    before = (out / "observer_gate_c_report.json").read_text(encoding="utf-8")
    assert run(root, out, "fit") == 0 and run(root, out, "fit", extra=("--force",)) == 0
    after = json.loads((out / "observer_gate_c_report.json").read_text(encoding="utf-8"))["stages"]["fit"]
    assert after["resumed_registered_stage"] is True and after["results"] == json.loads(before)["stages"]["fit"]["results"]
    assert json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))["n_hypotheses_ever"] == 28
    # CHANGED data for a registered hypothesis is refused (no best-of-N): same out, different backfill
    other = tmp_path / "other_backfill"
    for i, m in enumerate(("GER40", "NAS100")):
        make_market(other / m, G.load_prereg(G.DEFAULT_PREREG), 900 + i, 0.3)
    with pytest.raises(ST.HypothesisReuseError):
        run(other, out, "fit", extra=("--force",))


def test_validate_then_oos_once_with_frozen_cells(planted_run):
    out, root = planted_run["out"], planted_run["root"]
    fit = stage_of(out, "fit")
    assert run(root, out, "validate") == 0
    val = stage_of(out, "validate")
    assert val["partition"] == "VALIDATION" and val["verdict"] == G.V_VAL_OK and {r["key"] for r in val["results"] if r["kind"] == "hyp"} == set(fit["survivors"])
    assert all("gatec|validate|" in r["hypothesis"] for r in val["results"])
    vdefs, fdefs = val["markets"]["GER40"]["cell_defs"], fit["markets"]["GER40"]["cell_defs"]
    assert vdefs and all(vdefs[f] == fdefs[f] for f in vdefs)  # the stored TRAIN cells were applied unchanged, never refitted
    assert run(root, out, "oos") == 3  # touched exactly once: needs the explicit flag
    assert run(root, out, "oos", extra=("--confirm-oos-once",)) == 0
    oos = stage_of(out, "oos")
    assert oos["partition"] == "OOS" and oos["verdict"] == G.V_OOS_OK
    assert run(root, out, "oos", extra=("--confirm-oos-once",)) == 0  # a re-run only reproduces the registered numbers ...
    assert stage_of(out, "oos")["results"] == oos["results"]
    reg = json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert any(h.startswith("gatec|oos|") for h in reg["hypotheses"]) and any(h.startswith("gatec|validate|") for h in reg["hypotheses"])


def test_only_the_stage_partition_is_loaded(planted_run, prereg):
    cols = G.required_columns_from_task({"label": prereg.spec["label"], "contrasts": [{"feature": "f_acceptance__followthrough_dir_atr"}]})
    for part, lo, hi in (("TRAIN", 0, 90), ("VALIDATION", 90, 130), ("OOS", 130, 170)):
        df = G.load_stage_frame(planted_run["root"], "GER40", part, cols)
        assert set(df["partition"]) <= {part, "PURGED", "EMBARGO"}
        days = df.loc[df["partition"] == part, "decision_ts_ns"].to_numpy()
        assert days.min() >= T0 + lo * DAY_NS and days.max() < T0 + hi * DAY_NS + DAY_NS


# ---------------------------------------------------------------------------------------------- guards
def test_dry_run_reads_no_data(tmp_path, monkeypatch, capsys):
    import pyarrow.parquet as pq

    def boom(*a, **k):
        raise AssertionError("a dry run must not read any backfill data")

    monkeypatch.setattr(pd, "read_parquet", boom)
    monkeypatch.setattr(pq, "read_table", boom)
    monkeypatch.setattr(pq, "ParquetFile", boom)
    monkeypatch.setattr(pq, "read_schema", boom)
    rc = G.main(["--root", str(tmp_path / "no_such_backfill"), "--out", str(tmp_path / "o"), "--dry-run", "--markets", "GER40", "EURUSD"])
    txt = capsys.readouterr().out
    assert rc == 0 and "DRY RUN stage=fit" in txt and "tests: 28" in txt and "table.parquet=False" in txt and "nothing read" in txt
    assert not (tmp_path / "o").exists()  # no registry, no cache, no report from a dry run


def test_forward_period_is_refused(tmp_path, prereg):
    fwd_ts = int(pd.Timestamp("2026-09-02 10:00", tz="Europe/Berlin").value)
    root = tmp_path / "bf"
    make_market(root / "GER40", prereg, 3, 0.0, extra_ts_ns=fwd_ts)
    with pytest.raises(ForwardHoldoutError):
        run(root, tmp_path / "o", "fit", markets=("GER40",))
    assert not (tmp_path / "o" / "observer_gate_c_report.json").exists()
    with pytest.raises(SystemExit):  # there is no forward stage at all
        G.main(["--stage", "forward", "--root", str(root), "--out", str(tmp_path / "o2")])
    assert G.forward_start_ns() == int(pd.Timestamp("2026-09-01", tz="Europe/Berlin").value)


def test_out_inside_root_and_bad_jobs_are_refused(tmp_path, prereg):
    root = build_root(tmp_path, prereg, markets=("GER40",))
    with pytest.raises(SystemExit):
        G.main(["--root", str(root), "--out", str(root / "gate_c"), "--markets", "GER40"])
    with pytest.raises(SystemExit):
        G.main(["--root", str(root), "--out", str(tmp_path / "o"), "--jobs", "3"])
    with pytest.raises(ValueError):
        G.main(["--root", str(root), "--out", str(tmp_path / "o"), "--markets", "FOOBAR"])


def test_backfill_directory_is_never_written(planted_run):
    assert tree_hash(planted_run["root"]) == planted_run["before"]


def test_explore_scope_can_never_confirm(tmp_path, prereg):
    root = tmp_path / "bf"
    make_market(root / "BTCUSD", prereg, 7, 0.3)
    out = tmp_path / "o"
    assert run(root, out, "fit", markets=("BTCUSD",)) == 0
    st = stage_of(out, "fit")
    assert st["survivors"] == [] and st["verdict"] == G.V_NONE
    assert any(r["status"] == ST.SIGNIFICANT_ADJUSTED for r in st["results"] if r["id"] == "H01") and all(r["scope"] == "explore" for r in st["results"])
