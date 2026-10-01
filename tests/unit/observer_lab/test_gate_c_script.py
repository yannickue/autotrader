# ruff: noqa: E501
"""Gate C script (scripts/observer_gate_c.py, prereg-2) on SYNTHETIC mini backfills only: no real backfill file is opened by any test here.

Covers: preregistration parsing, preflight (manifest / version / fingerprint / balance, nothing registered on failure), the descriptive_only fallback, an
end-to-end fit -> validate -> oos chain, NC-A / NC-B / random negative controls, fit lock and report no-overwrite (no best-of-N), the full-core requirement,
single Holm family, NaN-draw abort, parallel == serial, dry run reads no data, forward period refused, read-only backfill directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
SPLIT = (("TRAIN", 0, 70), ("VALIDATION", 70, 115), ("FROZEN_OOS", 115, 160))
PER_DAY = 12
LABEL = "y_fav050_before_adv050"
PLANT_THRESHOLD = 0.43  # ~ 2/3 quantile of N(0,1): the top tercile of the planted feature
CORE2 = ("GER40", "NAS100")


def good_balance(**over) -> dict:
    e = {"match_rate": 0.97, "smd_local_minute": 0.02, "smd_atr_pct": 0.03, "smd_spread_pct": -0.04, "censoring_diff_pp": 0.4, "passed": True, "status": "passed"}
    e.update(over)
    return e


def manifest_for(version: str = "observer-controls-3", balance: dict | None = None, **top) -> dict:
    b = balance or good_balance()
    return {"control_method_version": version, "balance_gate": {"TRAIN": dict(b), "VALIDATION": dict(b), "FROZEN_OOS": dict(b)}, **top}


def raw_feature_columns(prereg: G.Prereg) -> list[str]:
    cols: list[str] = []
    for h in (*prereg.spec["hypotheses"], *prereg.spec["negative_controls"]):
        cols.extend(G.NEEDS.get(h["feature"], (h["feature"],)))
    return list(dict.fromkeys(cols))


def draw_features(rng: np.random.Generator, cols: list[str]) -> dict:
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
    return feats


def make_market(mdir: Path, prereg: G.Prereg, seed: int, planted: float, extra_ts_ns: int | None = None, ctlb_bias: float = 0.0, with_b: bool = True, with_v2: bool = False, manifest: dict | bool | None = True) -> None:
    rng = np.random.default_rng(seed)
    cols = raw_feature_columns(prereg)
    ev_rows, ct_rows, cb_rows, c2_rows = [], [], [], []
    for part, d0, d1 in SPLIT:
        for d in range(d0, d1):
            for k in range(PER_DAY):
                eid = f"e{d}_{k}"
                ts = T0 + d * DAY_NS + k * 300 * 10**9
                feats = draw_features(rng, cols)
                p = 0.5 + (planted if feats["f_acceptance__followthrough_dir_atr"] > PLANT_THRESHOLD else 0.0)
                y = float(rng.random() < p)
                base = {"direction": 1 if k % 2 == 0 else -1, "warmup_ok": True, "partition": part}
                ev_rows.append({"event_id": eid, "is_control": False, "control_of": None, "decision_ts_ns": ts, **base, LABEL: np.nan if rng.random() < 0.05 else y, **feats})
                for rows, pre, bias in ((ct_rows, "c_", 0.0), (cb_rows, "b_", ctlb_bias), (c2_rows, "v_", 0.0)):  # controls: same marginals, independent values
                    rows.append({"event_id": f"{pre}{eid}", "is_control": True, "control_of": eid, "decision_ts_ns": ts + 7 * 300 * 10**9, **base, LABEL: np.nan if rng.random() < 0.05 else float(rng.random() < 0.5 + bias), **draw_features(rng, cols)})
    for i in range(3):  # tags the lab drops and counts
        ev_rows.append({"event_id": f"p{i}", "is_control": False, "control_of": None, "decision_ts_ns": T0 + 69 * DAY_NS + 1000 * (i + 1), "direction": 1, "warmup_ok": True, "partition": "PURGED", LABEL: 1.0, **{c: v for c, v in ev_rows[0].items() if c in cols}})
    if extra_ts_ns is not None:
        ev_rows.append({**ev_rows[0], "event_id": "fwd0", "decision_ts_ns": extra_ts_ns, "partition": "FORWARD"})
    mdir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ev_rows).to_parquet(mdir / "table.parquet", index=False)
    pd.DataFrame(ct_rows).to_parquet(mdir / "controls.parquet", index=False)
    if with_b:
        pd.DataFrame(cb_rows).to_parquet(mdir / "controls_b.parquet", index=False)
    if with_v2:
        pd.DataFrame(c2_rows).to_parquet(mdir / "controls_v2.parquet", index=False)
    if manifest is not False and manifest is not None:
        man = manifest_for() if manifest is True else manifest
        (mdir / G.MANIFEST_NAME).write_text(json.dumps(man), encoding="utf-8")


@pytest.fixture(scope="module")
def real_prereg() -> G.Prereg:
    return G.load_prereg(G.DEFAULT_PREREG)


@pytest.fixture(scope="module")
def prereg_path(tmp_path_factory, real_prereg) -> Path:
    """The real preregistration with two core markets and a small B cap so that the synthetic runs are fast; everything else is the real block."""
    spec = json.loads(json.dumps(real_prereg.spec))
    spec["markets"]["core"] = list(CORE2)
    spec["test"]["b_min"], spec["test"]["b_max"] = 1000, 6000
    spec["stats"]["sensitivity_B_max"] = 1000
    p = tmp_path_factory.mktemp("prereg") / "prereg_small.md"
    p.write_text(f"{G.MARKER_BEGIN}\n```json\n{json.dumps(spec)}\n```\n{G.MARKER_END}\n", encoding="utf-8")
    return p


@pytest.fixture(scope="module")
def prereg(prereg_path) -> G.Prereg:
    return G.load_prereg(prereg_path)


@pytest.fixture(autouse=True)
def _lock_dir(request, tmp_path, monkeypatch):
    if "planted_run" not in request.fixturenames:  # tests on the shared fit use ITS lock directory (set by that fixture)
        monkeypatch.setattr(G, "LOCK_DIR", tmp_path / "locks")


def build_root(tmp_path: Path, prereg: G.Prereg, markets=CORE2, planted=0.3, seed=1, **kw) -> Path:
    root = tmp_path / "backfill"
    for i, m in enumerate(markets):
        make_market(root / m, prereg, seed + i, planted, **kw)
    return root


def run(root: Path, out: Path, prereg_path: Path, stage: str = "fit", jobs: int = 1, markets=CORE2, extra: tuple[str, ...] = ()) -> int:
    return G.main(["--root", str(root), "--out", str(out), "--stage", stage, "--jobs", str(jobs), "--markets", *markets, "--prereg", str(prereg_path), *extra])


def stage_of(out: Path, stage: str) -> dict:
    return json.loads((out / f"{G.REPORT_STEM}.json").read_text(encoding="utf-8"))["stages"][stage]


def tree_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def planted_run(tmp_path_factory, prereg, prereg_path):
    tmp = tmp_path_factory.mktemp("gc_planted")
    mp = pytest.MonkeyPatch()
    mp.setattr(G, "LOCK_DIR", tmp / "locks")  # the validate / oos tests below reuse this fit and its lock
    root = build_root(tmp, prereg, planted=0.3)
    before = tree_hash(root)
    out = tmp / "out_serial"
    assert run(root, out, prereg_path, "fit", jobs=1) == 0
    yield {"root": root, "out": out, "tmp": tmp, "before": before, "lock": tmp / "locks"}
    mp.undo()


# ---------------------------------------------------------------------------------------------- preregistration
def test_preregistration_parses_and_defines_the_family(real_prereg):
    s = real_prereg.spec
    assert s["prereg_version"] == "observer-gate-c-prereg-2" and s["supersedes"] == "observer-gate-c-prereg-1" and s["label"] == "y_fav050_before_adv050" and len(s["hypotheses"]) == 12
    assert s["registry_name"] == "observer_gate_c_prereg2" and s["stats"]["version"] == "observer-stats-2" and s["controls"]["required_method_version"] == "observer-controls-3"
    assert set(s["stages"]) == {"fit", "validate", "oos"} and "forward" not in json.dumps(s["stages"]).lower()
    assert not any("fib" in h["feature"].lower() for h in (*s["hypotheses"], *s["negative_controls"])), "Fibonacci is a separate placebo lane, not part of this family"
    assert real_prereg.file_sha256 and real_prereg.json_sha256 and G.market_scope(real_prereg, "GER40") == "core" and G.market_scope(real_prereg, "BTCUSD") == "explore"
    plan = G.build_plan(real_prereg, "fit", list(s["markets"]["core"]), None)
    fams = G.family_sizes(plan)
    lab = "y_fav050_before_adv050"
    # M3: ONE Holm family over all 60 hypotheses of the stage (not 20 / 10 / 15 / ... per feature group); the negative controls have their own families
    assert fams == {f"gatec2|fit|core|{lab}|all": 60, f"gatec2|fit|core|{lab}|ctrl": 10, f"gatec2|fit|core|{lab}|nc_a": 60, f"gatec2|fit|core|{lab}|nc_b": 60}
    ch = G.choose_B(real_prereg, fams)
    assert ch.B == 24000 and not ch.capped and not ST.is_power_limited(60, 24000)  # B >= 20 m / alpha for the largest family, no warning


def test_validate_and_oos_are_one_family_of_the_survivors(real_prereg):
    surv = ["GER40|H01", "NAS100|H05"]
    plan = G.build_plan(real_prereg, "validate", ["GER40", "NAS100", "EURUSD"], surv)
    fams = G.family_sizes(plan)
    assert fams["gatec2|validate|core|y_fav050_before_adv050|all"] == 2 and fams["gatec2|validate|core|y_fav050_before_adv050|nc_a"] == 2
    assert not any(p["market"] == "EURUSD" for p in plan)  # a market without survivors is not re-tested


def test_malformed_preregistration_is_refused(tmp_path, real_prereg):
    p = tmp_path / "bad.md"
    p.write_text("no block here", encoding="utf-8")
    with pytest.raises(ValueError):
        G.load_prereg(p)
    for mut in (lambda s: s["hypotheses"][0].update(feature="f_fib__level"), lambda s: s.pop("stats"), lambda s: s["test"].update(holm_scope="per_group"), lambda s: s["controls"]["balance"].pop("max_abs_smd"),
                lambda s: s["placebos"].pop("nc_b"), lambda s: s["placebos"].pop("alpha")):
        spec = json.loads(json.dumps(real_prereg.spec))
        mut(spec)
        p.write_text(f"{G.MARKER_BEGIN}\n```json\n{json.dumps(spec)}\n```\n{G.MARKER_END}", encoding="utf-8")
        with pytest.raises(ValueError):
            G.load_prereg(p)


# ---------------------------------------------------------------------------------------------- preflight (B2, M4) and the fallback
def no_side_effects(out: Path, prereg: G.Prereg) -> bool:
    return not (out / G.REGISTRY_FILE).exists() and not (out / f"{G.REPORT_STEM}.json").exists() and not G.lock_path(prereg).exists()


def test_preflight_only_registers_nothing_and_records_version_and_fingerprint(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg)
    out = tmp_path / "o"
    assert run(root, out, prereg_path, extra=("--preflight",)) == 0
    pre = json.loads((out / f"{G.PREFLIGHT_STEM}_fit.json").read_text(encoding="utf-8"))
    assert pre["ok"] and all(v["control_method_version"] == "observer-controls-3" and v["status"] == "passed" and len(v["data_fingerprint"]) == 64 and len(v["manifest_sha256"]) == 64 for v in pre["markets"].values())
    assert no_side_effects(out, prereg)


@pytest.mark.parametrize("case", ["no_manifest", "wrong_version", "bad_sha", "inconsistent_passed", "missing_field", "no_controls_b", "bad_json", "n_controls"])
def test_failing_preflight_registers_nothing_and_does_not_consume_the_stop_rule(tmp_path, prereg, prereg_path, case):
    root = build_root(tmp_path, prereg)
    mp = root / "GER40" / G.MANIFEST_NAME
    man = manifest_for()
    if case == "no_manifest":
        mp.unlink()
    elif case == "wrong_version":
        man["control_method_version"] = "observer-controls-2"
    elif case == "bad_sha":
        man["controls_sha256"] = "0" * 64
    elif case == "inconsistent_passed":
        man["balance_gate"]["TRAIN"]["match_rate"] = 0.5  # fails the preregistered gate but claims passed
    elif case == "missing_field":
        del man["balance_gate"]["TRAIN"]["smd_atr_pct"]
    elif case == "no_controls_b":
        (root / "GER40" / "controls_b.parquet").unlink()
    elif case == "n_controls":
        man["balance_gate"]["TRAIN"]["n_controls"] = 7
    if case == "bad_json":
        mp.write_text("{not json", encoding="utf-8")
    elif case not in ("no_manifest", "no_controls_b"):
        mp.write_text(json.dumps(man), encoding="utf-8")
    out = tmp_path / "o"
    assert run(root, out, prereg_path) == G.EXIT_PREFLIGHT
    assert no_side_effects(out, prereg)
    # the stop rule was not consumed: after the producer repairs the backfill the very same --out runs the fit
    make_market(root / "GER40", prereg, 1, 0.3)
    assert run(root, out, prereg_path) == 0 and stage_of(out, "fit")["status"] == "COMPLETE"


def test_fingerprint_is_content_based_not_size_or_mtime(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg, markets=("GER40",))
    fp = lambda: G.preflight(prereg, root, "fit", ["GER40"])["markets"]["GER40"]["data_fingerprint"]  # noqa: E731
    a = fp()
    for f in (root / "GER40").iterdir():
        os.utime(f, (1, 1))  # mtime changes, content does not
    assert fp() == a
    mp = root / "GER40" / G.MANIFEST_NAME
    man = json.loads(mp.read_text(encoding="utf-8"))
    man["balance_gate"]["TRAIN"]["match_rate"] = 0.98  # same size, other content
    mp.write_text(json.dumps(man), encoding="utf-8")
    assert fp() != a


def test_failed_balance_gate_is_descriptive_only_not_registered_and_not_in_m(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg)
    bad = good_balance(match_rate=0.55, passed=False, status="descriptive_only")
    (root / "NAS100" / G.MANIFEST_NAME).write_text(json.dumps(manifest_for(balance=bad)), encoding="utf-8")
    out = tmp_path / "o"
    assert run(root, out, prereg_path) == 0
    st = stage_of(out, "fit")
    assert st["descriptive_only_markets"] == ["NAS100"] and st["preflight"]["NAS100"]["status"] == "descriptive_only"
    nas = [r for r in st["results"] if r["market"] == "NAS100"]
    assert nas and all(r["kind"] == "hyp" and r["descriptive"] and r["survivor"] is False and r["adjusted_p"] is None and r["m_family"] is None for r in nas)
    assert {r["status"] for r in nas} <= {G.DESCRIPTIVE_ONLY, ST.INSUFFICIENT_EVIDENCE, G.CELL_UNDEFINED}
    ger_h = [r for r in st["results"] if r["market"] == "GER40" and r["kind"] == "hyp"]
    assert all(r["m_family"] == 12 for r in ger_h)  # m counts only the market that passed the gate
    reg = json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert not any("|NAS100|" in h for h in reg["hypotheses"])
    assert "GER40|H01" in st["survivors"] and not any(k.startswith("NAS100") for k in st["survivors"])
    assert "NOT 'no enrichment'" in (out / f"{G.REPORT_STEM}.md").read_text(encoding="utf-8")


def test_balance_gate_censoring_threshold_and_all_markets_descriptive_stops(tmp_path, prereg, prereg_path):
    assert G.balance_passes(prereg.spec["controls"]["balance"], good_balance()) is True
    assert G.balance_passes(prereg.spec["controls"]["balance"], good_balance(censoring_diff_pp=5.01)) is False
    assert G.balance_passes(prereg.spec["controls"]["balance"], good_balance(smd_atr_pct=-0.11)) is False
    root = build_root(tmp_path, prereg)
    bad = good_balance(censoring_diff_pp=6.0, passed=False, status="descriptive_only")
    for m in CORE2:
        (root / m / G.MANIFEST_NAME).write_text(json.dumps(manifest_for(balance=bad)), encoding="utf-8")
    out = tmp_path / "o"
    assert run(root, out, prereg_path) == G.EXIT_STOP
    assert not (out / G.REGISTRY_FILE).exists() and not G.lock_path(prereg).exists()


# ---------------------------------------------------------------------------------------------- end to end on synthetic data
def test_fit_finds_the_planted_cell_and_the_negative_controls_stay_quiet(planted_run):
    st = stage_of(planted_run["out"], "fit")
    assert st["status"] == "COMPLETE" and st["verdict"] == G.V_FIT_OK and st["negative_control_failures"] == [] and st["stats_version"] == "observer-stats-2"
    assert {"GER40|H01", "NAS100|H01"} <= set(st["survivors"])
    res = {r["key"] + "|" + r["id"]: r for r in st["results"]}
    h01 = res["GER40|H01|H01"]
    assert h01["status"] == ST.SIGNIFICANT_ADJUSTED and h01["sign_check"] == "EXPECTED" and h01["delta"] > 0.1 and h01["m_family"] == 24 and h01["B"] == st["B"] == 6000 and h01["family"].endswith("|all")
    assert h01["n_blocks_event"] >= 30 and h01["n_blocks_control"] >= 30 and h01["n_nan_draws"] == 0 and h01["week_ci_excludes_0"] is True  # per-arm blocks; week sensitivity agrees
    assert h01["base_delta"] > 0.05  # events differ from controls (the planted effect), yet the information-free controls below stay null
    for kind, n_per_market in (("nc", 2), ("nc_a", 12), ("nc_b", 12)):
        rows = [r for r in st["results"] if r["kind"] == kind]
        assert len(rows) == 2 * n_per_market and all(r["status"] != ST.SIGNIFICANT_ADJUSTED and not r["survivor"] for r in rows), kind
        assert any(r["status"] == ST.NOT_SIGNIFICANT for r in rows)  # actually evaluated, not just insufficient
    for m in CORE2:
        v = st["markets"][m]
        assert v["nc_a_base"]["null"] is True and v["nc_a_info"]["n_pairs"] > 500 and v["nc_b_coverage"] > 0.8
        assert st["preflight"][m]["control_method_version"] == "observer-controls-3" and v["counts"]["n_purged_embargo_rows"] == 3
        assert v["cell_defs"]["f_acceptance__followthrough_dir_atr"]["kind"] == "quantile"
    assert set(st["families"]) == {f"gatec2|fit|core|{LABEL}|{k}" for k in ("all", "ctrl", "nc_a", "nc_b")}
    assert (planted_run["out"] / f"{G.REPORT_STEM}.md").read_text(encoding="utf-8").count("GER40") > 5


def test_unplanted_data_gives_no_enrichment_and_the_stop_rule_blocks_the_next_stage(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg, planted=0.0, seed=50)
    out = tmp_path / "out"
    assert run(root, out, prereg_path, "fit") == 0
    st = stage_of(out, "fit")
    assert st["verdict"] in (G.V_NONE, G.V_FIT_OK) and st["negative_control_failures"] == []
    if st["verdict"] == G.V_NONE:  # (a nominal 5 % false-positive chance exists; with this fixed seed the null stays null)
        assert st["survivors"] == []
        assert run(root, out, prereg_path, "validate") == G.EXIT_STOP
        assert "validate" not in json.loads((out / f"{G.REPORT_STEM}.json").read_text(encoding="utf-8"))["stages"]


def test_parallel_equals_serial_bit_identical(planted_run, prereg_path, tmp_path, monkeypatch):
    monkeypatch.setattr(G, "LOCK_DIR", tmp_path / "locks2")  # an independent fit (the first one is locked)
    out2 = tmp_path / "out_parallel"
    assert run(planted_run["root"], out2, prereg_path, "fit", jobs=2) == 0
    a, b = stage_of(planted_run["out"], "fit"), stage_of(out2, "fit")
    for k in ("results", "markets", "families", "survivors", "verdict", "B", "n_hypotheses_ever", "preflight"):
        assert a[k] == b[k], k
    ra = json.loads((planted_run["out"] / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    rb = json.loads((out2 / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert {h: v["p"] for h, v in ra["hypotheses"].items()} == {h: v["p"] for h, v in rb["hypotheses"].items()}
    assert b["timing"]["jobs"] == 2 and sorted(b["timing"]["evaluated_markets"]) == list(CORE2)


# ---------------------------------------------------------------------------------------------- registry, fit lock, no best-of-N (M1, M2)
def test_registry_is_separate_persistent_and_forbids_best_of_n(planted_run, prereg, prereg_path, tmp_path, monkeypatch):
    out, root = planted_run["out"], planted_run["root"]
    reg = json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert reg["name"] == "observer_gate_c_prereg2" and reg["n_hypotheses_ever"] == 2 * (12 + 2 + 12 + 12) and all(h.startswith("gatec2|fit|") for h in reg["hypotheses"])
    assert len(reg["families"]) == 4 and all(v["n_results_recorded_at_declaration"] == 0 for v in reg["families"].values())  # declared before any result existed
    with pytest.raises(ValueError):  # a registry of another name (e.g. prereg-1, the lab's or Gate B's) can never be mixed in
        ST.HypothesisRegistry("observer_gate_c", out / G.REGISTRY_FILE)
    # an identical re-run (cache hit) reproduces the registered results; a forced recompute of identical data is bit-identical, so it is accepted too
    before = json.loads((out / f"{G.REPORT_STEM}.json").read_text(encoding="utf-8"))["stages"]["fit"]["results"]
    assert run(root, out, prereg_path) == 0 and run(root, out, prereg_path, extra=("--force",)) == 0
    after = stage_of(out, "fit")
    assert after["resumed_registered_stage"] is True and after["results"] == before
    assert json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))["n_hypotheses_ever"] == 2 * 38
    # CHANGED data with the same --out: refused by the fit lock (data fingerprint), no new numbers (a COPY of the lock, the shared one stays untouched)
    shutil.copytree(planted_run["lock"], tmp_path / "lock_copy")
    monkeypatch.setattr(G, "LOCK_DIR", tmp_path / "lock_copy")
    other = tmp_path / "other_backfill"
    for i, m in enumerate(CORE2):
        make_market(other / m, prereg, 900 + i, 0.3)
    assert run(other, out, prereg_path, extra=("--force",)) == G.EXIT_STOP
    # ... and even with the lock removed the registry itself refuses to replace the registered numbers
    G.lock_path(prereg).unlink()
    with pytest.raises(ST.HypothesisReuseError):
        run(other, out, prereg_path, extra=("--force",))


def test_a_second_fit_with_another_out_is_refused_and_the_report_is_never_overwritten(planted_run, prereg, prereg_path, tmp_path):
    root = planted_run["root"]
    lock = json.loads(G.lock_path(prereg).read_text(encoding="utf-8"))
    assert lock["out"] == str(planted_run["out"].resolve()) and lock["fit_stage_sha256"] and lock["prereg_version"] == "observer-gate-c-prereg-2"
    assert lock["fit_stage_sha256"] == G.stage_hash(stage_of(planted_run["out"], "fit"))
    out2 = tmp_path / "second_fit"
    assert run(root, out2, prereg_path, "fit") == G.EXIT_STOP  # best-of-N via a new --out
    assert not (out2 / G.REGISTRY_FILE).exists() and not (out2 / f"{G.REPORT_STEM}.json").exists()
    fit = stage_of(planted_run["out"], "fit")
    tampered = json.loads(json.dumps(fit))
    tampered["results"][0]["delta"] = 0.123
    with pytest.raises(G.StopRule):
        G.write_report(planted_run["out"], prereg, "fit", tampered)  # M2: stages[fit] is never overwritten with different content
    assert stage_of(planted_run["out"], "fit") == fit


def test_fit_needs_the_full_core_set(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg)
    with pytest.raises(SystemExit):
        run(root, tmp_path / "o", prereg_path, "fit", markets=("GER40",))
    assert no_side_effects(tmp_path / "o", prereg)


def test_validate_then_oos_once_with_frozen_cells(planted_run, prereg_path):
    out, root = planted_run["out"], planted_run["root"]
    fit = stage_of(out, "fit")
    assert run(root, out, prereg_path, "validate") == 0
    val = stage_of(out, "validate")
    assert val["partition"] == "VALIDATION" and val["verdict"] == G.V_VAL_OK and {r["key"] for r in val["results"] if r["kind"] == "hyp"} == set(fit["survivors"])
    assert all("gatec2|validate|" in r["hypothesis"] for r in val["results"])
    assert {r["family"] for r in val["results"] if r["kind"] == "hyp"} == {f"gatec2|validate|core|{LABEL}|all"} and all(r["m_family"] == len(fit["survivors"]) for r in val["results"] if r["kind"] == "hyp")
    vdefs, fdefs = val["markets"]["GER40"]["cell_defs"], fit["markets"]["GER40"]["cell_defs"]
    assert vdefs and all(vdefs[f] == fdefs[f] for f in vdefs)  # the stored TRAIN cells were applied unchanged, never refitted
    assert run(root, out, prereg_path, "oos") == G.EXIT_STOP  # touched exactly once: needs the explicit flag
    assert run(root, out, prereg_path, "oos", extra=("--confirm-oos-once",)) == 0
    oos = stage_of(out, "oos")
    assert oos["partition"] == "OOS" and oos["verdict"] == G.V_OOS_OK
    assert run(root, out, prereg_path, "oos", extra=("--confirm-oos-once",)) == 0  # a re-run only reproduces the registered numbers ...
    assert stage_of(out, "oos")["results"] == oos["results"]
    reg = json.loads((out / G.REGISTRY_FILE).read_text(encoding="utf-8"))
    assert any(h.startswith("gatec2|oos|") for h in reg["hypotheses"]) and any(h.startswith("gatec2|validate|") for h in reg["hypotheses"])


def test_validate_refuses_when_the_fit_lock_does_not_match(planted_run, prereg, prereg_path, tmp_path):
    out2 = tmp_path / "copy_out"
    out2.mkdir()
    (out2 / f"{G.REPORT_STEM}.json").write_text((planted_run["out"] / f"{G.REPORT_STEM}.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert run(planted_run["root"], out2, prereg_path, "validate") == G.EXIT_STOP  # a copied fit report under another --out is not the locked one


def test_only_the_stage_partition_is_loaded(planted_run, prereg):
    cols = G.required_columns_from_task({"label": prereg.spec["label"], "contrasts": [{"feature": "f_acceptance__followthrough_dir_atr"}]})
    for part, lo, hi in (("TRAIN", 0, 70), ("VALIDATION", 70, 115), ("OOS", 115, 160)):
        df = G.load_stage_frame(planted_run["root"], "GER40", part, cols)
        assert set(df["partition"]) <= {part, "PURGED", "EMBARGO"}
        days = df.loc[df["partition"] == part, "decision_ts_ns"].to_numpy()
        assert days.min() >= T0 + lo * DAY_NS and days.max() < T0 + hi * DAY_NS + DAY_NS


# ---------------------------------------------------------------------------------------------- negative controls
def test_a_a_test_fails_the_stage_when_the_second_control_set_differs(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg, planted=0.3, ctlb_bias=0.2)  # controls_b is NOT interchangeable with controls: matching or generation is broken
    out = tmp_path / "o"
    assert run(root, out, prereg_path) == 0
    st = stage_of(out, "fit")
    assert st["verdict"] == G.V_INVALID and st["survivors"] == [] and any(f.endswith("|NC_A_BASE_NOT_NULL") for f in st["negative_control_failures"])
    assert run(root, out, prereg_path, "validate") == G.EXIT_STOP  # stop rule: the stage is invalid, validate is not run


def _row(kind, status, market="GER40", **kw):
    return {"key": f"{market}|{kind}", "kind": kind, "market": market, "status": status, "survivor": False, "descriptive": False, **kw}


def test_stage_verdict_is_fail_closed_for_negative_controls():
    fams = {"gatec2|fit|core|y|all": {"m": 60, "power_limited": False}}
    mk = {"GER40": {"nc_a_base": {"null": True}}}
    ok = [_row("hyp", ST.NOT_SIGNIFICANT), _row("nc_a", ST.NOT_SIGNIFICANT), _row("nc_b", ST.NOT_SIGNIFICANT)]
    assert G.stage_verdict("fit", ok, mk, fams)[0] == G.V_NONE
    for bad, why in (([*ok, _row("nc_b", ST.SIGNIFICANT_ADJUSTED)], "nc_b significant"), ([*ok, _row("nc", ST.SIGNIFICANT_ADJUSTED)], "random control significant"),
                     ([_row("hyp", ST.NOT_SIGNIFICANT), _row("nc_a", ST.INSUFFICIENT_EVIDENCE), _row("nc_b", ST.NOT_SIGNIFICANT)], "A/A test not evaluable"),
                     ([_row("hyp", ST.NOT_SIGNIFICANT), _row("nc_a", ST.NOT_SIGNIFICANT), _row("nc_b", G.CELL_UNDEFINED)], "shift placebo not evaluable")):
        v, surv, fails = G.stage_verdict("fit", bad, mk, fams)
        assert v == G.V_INVALID and surv == [] and fails, why
    assert G.stage_verdict("fit", ok, {"GER40": {"nc_a_base": {"null": False}}}, fams)[0] == G.V_INVALID
    assert G.stage_verdict("fit", ok, mk, {"gatec2|fit|core|y|nc_b": {"m": 60, "power_limited": True}})[0] == G.V_INVALID  # a power-limited placebo family proves nothing


def test_shift_placebo_construction():
    ts = [T0 + d * DAY_NS + k * 600 * 10**9 for d in range(4) for k in range(3)]
    df = pd.DataFrame({"decision_ts_ns": ts, "partition": "TRAIN", "is_control": False, "direction": 1, LABEL: np.arange(12, dtype=float)})
    df[G.DAY_COL] = G.berlin_day_ordinal(df["decision_ts_ns"].to_numpy(dtype="int64"))
    new, cov = G.shifted_labels(df, LABEL, "TRAIN", 1800)
    assert list(new[:3]) == [3.0, 4.0, 5.0] and list(new[3:6]) == [6.0, 7.0, 8.0] and np.isnan(new[9:]).all() and cov == pytest.approx(9 / 12)  # same time of day, next day; last day has no next day
    assert G.shifted_labels(df, LABEL, "TRAIN", 60)[1] == pytest.approx(9 / 12)  # exact bars also match here
    ctl = df.copy()
    ctl["is_control"] = True
    both = pd.concat([df, ctl.assign(**{LABEL: 100.0 + np.arange(12)})], ignore_index=True)
    nb, _ = G.shifted_labels(both, LABEL, "TRAIN", 1800)
    assert nb[0] == 3.0 and nb[12] == 103.0  # arms never mix


def test_nc_a_pairing_blocks_by_the_original_event():
    ev = pd.DataFrame({"event_id": ["e1", "e2"], "is_control": False, "control_of": None, "partition": "TRAIN", G.DAY_COL: [10, 11]})
    c1 = pd.DataFrame({"event_id": ["c1a", "c1b", "c2a"], "is_control": True, "control_of": ["e1", "e1", "e2"], "partition": "TRAIN", G.DAY_COL: [10, 12, 11]})
    cb = pd.DataFrame({"event_id": ["b1a", "b2a", "b2b"], "is_control": True, "control_of": ["e1", "e2", "e2"], "partition": "TRAIN", G.DAY_COL: [10, 11, 11]})
    frame, info = G.build_nc_a_frame(pd.concat([ev, c1], ignore_index=True), cb, "TRAIN")
    assert info["n_pairs"] == 2 and info["n_unpaired_dropped"] == 1
    pe, cc = frame[~frame["is_control"]], frame[frame["is_control"]]
    assert set(pe["event_id"]) == {"b::b1a", "b::b2a"} and set(cc["control_of"]) == set(pe["event_id"])
    assert dict(zip(cc["event_id"], cc["control_of"], strict=True)) == {"c1a": "b::b1a", "c2a": "b::b2a"}  # rank pairing inside the event
    assert set(pe[G.DAY_COL]) == {10, 11} and set(cc[G.DAY_COL]) == {10, 11}  # the day of the ORIGINAL event (c1b, a control on day 12, was dropped as unpaired)


# ---------------------------------------------------------------------------------------------- NaN draws, bridge, explore
def test_nan_bootstrap_draws_abort_the_stage(tmp_path, prereg, prereg_path, monkeypatch):
    from dataclasses import replace

    real = ST.block_bootstrap_contrast
    monkeypatch.setattr(ST, "block_bootstrap_contrast", lambda *a, **k: replace(real(*a, **k), n_nan_draws=3))
    root = build_root(tmp_path, prereg)
    out = tmp_path / "o"
    assert run(root, out, prereg_path) == G.EXIT_NAN
    assert not (out / f"{G.REPORT_STEM}.json").exists()  # nothing reported as a result


def test_explore_scope_can_never_confirm_and_nc_c_bridge_is_descriptive(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg)
    make_market(root / "BTCUSD", prereg, 7, 0.3, with_b=False, with_v2=True)
    out = tmp_path / "o"
    assert run(root, out, prereg_path, "fit", markets=(*CORE2, "BTCUSD")) == 0
    st = stage_of(out, "fit")
    btc = [r for r in st["results"] if r["market"] == "BTCUSD"]
    assert btc and all(r["scope"] == "explore" and r["kind"] in ("hyp", "nc") and not r["survivor"] for r in btc) and not any(k.startswith("BTCUSD") for k in st["survivors"])
    assert any(r["status"] == ST.SIGNIFICANT_ADJUSTED for r in btc if r["id"] == "H01")
    assert {r["family"] for r in btc if r["kind"] == "hyp"} == {f"gatec2|fit|explore|{LABEL}|all"}
    br = st["markets"]["BTCUSD"]["bridge"]
    assert br["available"] and br["n_controls3"] > 500 and abs(br["difference_3_minus_2"]) < 0.2 and br["note"].startswith("descriptive only")
    assert st["markets"]["GER40"]["bridge"] is None


# ---------------------------------------------------------------------------------------------- guards
def test_dry_run_reads_no_data(tmp_path, monkeypatch, capsys, real_prereg):
    import pyarrow.parquet as pq

    def boom(*a, **k):
        raise AssertionError("a dry run must not read any backfill data")

    monkeypatch.setattr(pd, "read_parquet", boom)
    monkeypatch.setattr(pq, "read_table", boom)
    monkeypatch.setattr(pq, "ParquetFile", boom)
    monkeypatch.setattr(pq, "read_schema", boom)
    rc = G.main(["--root", str(tmp_path / "no_such_backfill"), "--out", str(tmp_path / "o"), "--dry-run", "--markets", "GER40", "EURUSD"])
    txt = capsys.readouterr().out
    assert rc == 0 and "DRY RUN stage=fit" in txt and "tests: 76" in txt and "table.parquet=False" in txt and "controls_manifest.json=False" in txt and "nothing read" in txt and "REFUSED" in txt
    assert not (tmp_path / "o").exists()  # no registry, no cache, no report from a dry run


def test_forward_period_is_refused_before_anything_is_registered(tmp_path, prereg, prereg_path):
    fwd_ts = int(pd.Timestamp("2026-09-02 10:00", tz="Europe/Berlin").value)
    root = build_root(tmp_path, prereg, markets=("NAS100",))
    make_market(root / "GER40", prereg, 3, 0.0, extra_ts_ns=fwd_ts)
    with pytest.raises(ForwardHoldoutError):
        run(root, tmp_path / "o", prereg_path, "fit")
    assert no_side_effects(tmp_path / "o", prereg)
    with pytest.raises(SystemExit):  # there is no forward stage at all
        G.main(["--stage", "forward", "--root", str(root), "--out", str(tmp_path / "o2")])
    assert G.forward_start_ns() == int(pd.Timestamp("2026-09-01", tz="Europe/Berlin").value)


def test_out_inside_root_and_bad_jobs_are_refused(tmp_path, prereg, prereg_path):
    root = build_root(tmp_path, prereg, markets=("GER40",))
    with pytest.raises(SystemExit):
        G.main(["--root", str(root), "--out", str(root / "gate_c"), "--markets", "GER40", "--prereg", str(prereg_path)])
    with pytest.raises(SystemExit):
        G.main(["--root", str(root), "--out", str(tmp_path / "o"), "--jobs", "3", "--prereg", str(prereg_path)])
    with pytest.raises(ValueError):
        G.main(["--root", str(root), "--out", str(tmp_path / "o"), "--markets", "FOOBAR", "--prereg", str(prereg_path)])


def test_backfill_directory_is_never_written(planted_run):
    assert tree_hash(planted_run["root"]) == planted_run["before"]
