# ruff: noqa: E501
"""scripts/run_tests.py: the existing tiers (fast / integration / safety / slow / full) are unchanged; the impact mapping is only ever wider than before; T0-T3 names;
the opt-in green-result cache never serves safety / integration / slow suites or -m selections and invalidates on any content change."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def rt():
    spec = importlib.util.spec_from_file_location("run_tests_under_test", REPO / "scripts" / "run_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _dry(rt, monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["run_tests.py", *argv, "--dry-run"])
    rc = rt.main()
    out = capsys.readouterr().out
    cmds = [ln[2:].split("pytest ", 1)[1].replace("-n 2 --dist loadgroup ", "") for ln in out.splitlines() if ln.startswith("+ ") and "pytest" in ln]
    return rc, cmds


# ---------------------------------------------------------------------------------------------- the existing tiers do not change
def test_existing_tier_definitions_are_unchanged(rt):
    assert rt.SEGMENTS == {"fast": ["-m", "fast"], "integration": ["-m", "integration"], "safety": ["-m", "safety"], "slow": ["-m", "slow"]}
    assert rt.SEGMENT_TIMEOUT_S == {"fast": 600, "integration": 1200, "safety": 1500, "slow": 2400}
    assert rt.DEFAULT_WORKERS == {"fast": 2, "integration": 0, "safety": 0, "slow": 2}


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("fast", ["-q --durations=15 -m fast"]),
        ("integration", ["-q --durations=15 -m integration"]),
        ("safety", ["-q --durations=15 -m safety"]),
        ("slow", ["-q --durations=15 -m slow"]),
        ("full", ["-q --durations=15 -m fast", "-q --durations=15 -m integration", "-q --durations=15 -m slow"]),
    ],
)
def test_existing_commands_produce_the_same_pytest_invocations(rt, monkeypatch, capsys, cmd, expected):
    rc, cmds = _dry(rt, monkeypatch, capsys, cmd)
    assert rc == 0 and cmds == expected


def test_named_tiers_compose_the_existing_segments(rt, monkeypatch, capsys):
    _, t2 = _dry(rt, monkeypatch, capsys, "t2")
    assert t2 == ["-q --durations=15 -m integration", "-q --durations=15 -m safety"]
    _, t3 = _dry(rt, monkeypatch, capsys, "t3")
    assert t3 == ["-q --durations=15 -m fast", "-q --durations=15 -m integration", "-q --durations=15 -m slow", "-q --durations=15 -m safety"]
    _, t1 = _dry(rt, monkeypatch, capsys, "t1", "src/risk/policy.py")
    _, ch = _dry(rt, monkeypatch, capsys, "changed", "src/risk/policy.py")
    assert t1 == ch and t1


# ---------------------------------------------------------------------------------------------- impact mapping: explicit, documented, never narrower
def _expand(rt, targets):
    out = []
    for t in targets:
        out += sorted(x.relative_to(REPO).as_posix() for x in REPO.glob(t)) if "*" in t else [t]
    return out


def test_every_pre_existing_matrix_rule_is_still_selected(rt):
    for prefix, targets in rt.MATRIX:
        if prefix == "tests/":
            continue
        plan = rt.plan_for([prefix + "some_module.py"])
        flat = [t for inv in plan for t in inv]
        for t in _expand(rt, [x for x in targets if not x.startswith("m:")]):
            assert t in flat, (prefix, t)


def test_observer_paths_select_observer_parity_leakage_store_and_research_tests(rt):
    for p in ("src/market_observer/observer.py", "src/coverage_analysis/observer_lab/backfill.py", "src/research_speed/segments.py", "scripts/observer_backfill.py"):
        flat = [t for inv in rt.plan_for([p]) for t in inv]
        for need in ("tests/unit/market_observer", "tests/unit/observer_lab", "tests/unit/demo/test_observer_parity.py", "tests/unit/demo/test_observer_store.py", "tests/unit/research"):
            assert need in flat, (p, need)
    assert (REPO / "tests/unit/market_observer/test_negative_control_leaky.py").is_file()  # leakage suite lives below tests/unit/market_observer


def test_execution_risk_and_exits_mappings(rt):
    ex = rt.impact_plan(["src/execution/orders.py"])
    assert {"tests/unit/execution", "tests/unit/persistence", "tests/unit/risk", "tests/contracts"} <= set(ex.targets) and "safety" in ex.marks and any("canary" in n for n in ex.notes)
    rk = rt.impact_plan(["src/risk/policy.py"])
    assert {"tests/unit/risk", "tests/unit/portfolio", "tests/contracts"} <= set(rk.targets) and "safety" in rk.marks
    xt = rt.impact_plan(["src/exits/engine.py"])
    assert {"tests/unit/exits", "tests/unit/execution", "tests/unit/demo/execution", "tests/integration"} <= set(xt.targets)


def test_docs_only_changes_select_no_pytest(rt, monkeypatch, capsys):
    ip = rt.impact_plan(["docs/RESEARCH_SPEED.md", "README.md", "reports/x.txt"])
    assert ip.docs_only and rt.plan_for(["docs/RESEARCH_SPEED.md"]) == []
    assert not rt.impact_plan(["docs/a.md", "src/risk/policy.py"]).docs_only


def test_unknown_paths_widen_and_never_select_less(rt):
    for p in ("src/brand_new_pkg/mod.py", "Makefile", "somewhere/else.cfg"):
        ip = rt.impact_plan([p])
        assert "fast or integration or safety" in ip.marks and p in ip.widened
        assert rt.plan_for([p]) == [["-m", "fast or integration or safety"]]


def test_global_changes_select_the_segmented_full_suite(rt):
    for p in ("pyproject.toml", "uv.lock", "tests/conftest.py", "tests/_shared_helper.py"):
        assert rt.plan_for([p]) == [["-m", "fast"], ["-m", "integration"], ["-m", "slow"]]


def test_test_helper_changes_select_their_directory(rt):
    assert ["tests/unit/observer_lab"] in rt.plan_for(["tests/unit/observer_lab/ol_backfill_support.py"])
    assert rt.plan_for(["tests/unit/risk/test_x.py"]) == [["tests/unit/risk/test_x.py"]]


# ---------------------------------------------------------------------------------------------- green-result cache (opt-in, validated, conservative)
@pytest.fixture
def fake_repo(tmp_path, monkeypatch, rt):
    import test_result_cache as trc

    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "scripts").mkdir()
    (r / "configs").mkdir()
    (r / "tests" / "unit" / "foo").mkdir(parents=True)
    (r / "tests" / "unit" / "risk").mkdir(parents=True)
    (r / "tests" / "unit" / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "tests" / "conftest.py", r / "tests" / "conftest.py")
    (r / "pyproject.toml").write_text("[project]\nname='x'\n")
    (r / "uv.lock").write_text("lock1\n")
    (r / "configs" / "a.toml").write_text("k=1\n")
    (r / "src" / "foo_mod.py").write_text("from foo_dep import y\n\n\ndef f():\n    return y\n")
    (r / "src" / "foo_dep.py").write_text("y = 1\n")
    (r / "tests" / "unit" / "foo" / "test_a.py").write_text("from foo_mod import f\n\n\ndef test_a():\n    assert f() == 1\n")
    (r / "tests" / "unit" / "risk" / "test_s.py").write_text("def test_s():\n    pass\n")
    (r / "tests" / "unit" / "scripts" / "test_i.py").write_text("def test_i():\n    pass\n")
    monkeypatch.setattr(trc, "ROOT", r)
    monkeypatch.setenv("TEST_RESULT_CACHE_DIR", str(tmp_path / "cache"))
    return r, trc


def test_cache_hit_only_for_identical_content_and_names_nothing_stale(fake_repo):
    r, trc = fake_repo
    t = ["tests/unit/foo/test_a.py"]
    c, why = trc.candidate(t, [])
    assert c is not None and not why and trc.lookup(c) is None  # nothing green yet
    trc.store(c, ["pytest"], 3.0)
    again, _ = trc.candidate(t, [])
    assert trc.lookup(again) is not None and again.fingerprint == c.fingerprint
    variants = {
        "test file": lambda: (r / "tests/unit/foo/test_a.py").write_text("from foo_mod import f\n\n\ndef test_a():\n    assert f() == 2\n"),
        "transitive src": lambda: (r / "src/foo_dep.py").write_text("y = 2\n"),
        "conftest": lambda: (r / "tests/conftest.py").write_text((r / "tests/conftest.py").read_text() + "\n# edit\n"),
        "pyproject": lambda: (r / "pyproject.toml").write_text("[project]\nname='y'\n"),
        "lock": lambda: (r / "uv.lock").write_text("lock2\n"),
        "configs": lambda: (r / "configs/a.toml").write_text("k=2\n"),
    }
    original = {k: None for k in variants}
    for name, mutate in variants.items():
        snap = {p: p.read_text() for p in (r / "tests/unit/foo/test_a.py", r / "src/foo_dep.py", r / "tests/conftest.py", r / "pyproject.toml", r / "uv.lock", r / "configs/a.toml")}
        mutate()
        cc, _ = trc.candidate(t, [])
        assert cc is not None and trc.lookup(cc) is None and cc.fingerprint != c.fingerprint, name
        for p, txt in snap.items():
            p.write_text(txt)
        assert trc.lookup(trc.candidate(t, [])[0]) is not None, name  # restoring the content restores the hit
    del original
    other_args, _ = trc.candidate(t, ["-x"])
    assert other_args.fingerprint != c.fingerprint and trc.lookup(other_args) is None


def test_cache_never_serves_safety_integration_marker_selections_or_a_failed_run(fake_repo):
    _r, trc = fake_repo
    for t, word in ((["tests/unit/risk/test_s.py"], "safety"), (["tests/unit/scripts/test_i.py"], "integration")):
        c, why = trc.candidate(t, [])
        assert c is None and word in why
    for t in (["-m", "safety"], ["tests/unit/foo/test_a.py::test_a"], ["tests/unit/foo/test_*.py"], ["tests/unit/nope/test_x.py"]):
        c, why = trc.candidate(t, [])
        assert c is None and why
    c, _ = trc.candidate(["tests/unit/foo"], [])  # a directory expands to its test files
    assert c is not None and c.files == ("tests/unit/foo/test_a.py",)
    assert trc.lookup(c) is None  # a failed run is simply never stored; a record with rc != 0 is not a hit either
    rec = {"schema": trc.CACHE_SCHEMA, "fingerprint": c.fingerprint, "files": list(c.files), "rc": 1, "cmd": [], "wall_s": 1, "green_utc": "x"}
    Path(trc.cache_dir(), f"{c.fingerprint}.json").parent.mkdir(parents=True, exist_ok=True)
    Path(trc.cache_dir(), f"{c.fingerprint}.json").write_text(json.dumps(rec))
    assert trc.lookup(c) is None


def test_runner_uses_the_cache_only_when_asked_and_never_for_mark_selections(rt, fake_repo, monkeypatch, capsys):
    r, _trc = fake_repo
    calls: list[list[str]] = []
    monkeypatch.setattr(rt, "run", lambda cmd, timeout, dry: calls.append(cmd) or 0)
    plan = [["tests/unit/foo/test_a.py"]]
    assert rt._run_planned(plan, [], False, True) == 0 and len(calls) == 1  # miss -> runs, green -> stored
    assert rt._run_planned(plan, [], False, True) == 0 and len(calls) == 1 and "CACHE HIT" in capsys.readouterr().out  # hit -> not re-run
    assert rt._run_planned(plan, [], False, False) == 0 and len(calls) == 2  # cache OFF (default): always runs
    assert rt._run_planned([["-m", "safety"]], [], False, True) == 0 and len(calls) == 3  # -m selections never cached
    assert rt._run_planned([["-m", "safety"]], [], False, True) == 0 and len(calls) == 4
    monkeypatch.setattr(rt, "run", lambda cmd, timeout, dry: calls.append(cmd) or 1)
    (r / "src/foo_dep.py").write_text("y = 5\n")
    n = len(calls)
    assert rt._run_planned(plan, [], False, True) == 1 and len(calls) == n + 1
    monkeypatch.setattr(rt, "run", lambda cmd, timeout, dry: calls.append(cmd) or 0)
    assert rt._run_planned(plan, [], False, True) == 0 and len(calls) == n + 2  # the failed run was not stored: re-run, now green


def test_exits_changes_also_select_the_safety_overlay_and_never_less_than_before(rt):
    xt = rt.impact_plan(["src/exits/engine.py"])
    assert "safety" in xt.marks
    assert {"tests/unit/exits", "tests/unit/execution", "tests/unit/demo/execution", "tests/integration"} <= set(xt.targets)
    assert ["-m", "safety"] in rt.plan_for(["src/exits/engine.py"])


def test_dynamic_imports_make_a_test_uncacheable_so_a_changed_dynamic_dependency_is_always_a_miss(fake_repo):
    r, trc = fake_repo
    (r / "src" / "dyn_dep.py").write_text("z = 1\n")
    (r / "src" / "dyn_mod.py").write_text("import importlib\n\n\ndef g():\n    return importlib.import_module('dyn_dep').z\n")
    (r / "tests" / "unit" / "foo" / "test_dyn.py").write_text("from dyn_mod import g\n\n\ndef test_d():\n    assert g() >= 1\n")
    (r / "tests" / "unit" / "foo" / "test_direct.py").write_text("def test_d():\n    m = __import__('dyn_dep')\n    assert m.z\n")
    (r / "tests" / "unit" / "foo" / "test_spec.py").write_text("import importlib.util\n\n\ndef test_d():\n    s = importlib.util.spec_from_file_location('x', 'y.py')\n    assert s is None or s\n")
    for f in ("test_dyn.py", "test_direct.py", "test_spec.py"):
        c, why = trc.candidate([f"tests/unit/foo/{f}"], [])
        assert c is None and "dynamic import" in why, (f, why)
    c, _ = trc.candidate(["tests/unit/foo/test_a.py"], [])  # a static closure is still cached
    assert c is not None
