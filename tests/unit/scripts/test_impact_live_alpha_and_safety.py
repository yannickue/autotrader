# ruff: noqa: E501
"""Impact matrix guarantees (never narrower) + the production runtime import manifest + the tier classification of the observer parity suite.

Port of the semantics of dc6346c onto the newer scripts/impact_tests.py (EXTRA_RULES union), with the private import-closure copy replaced by
scripts/runtime_import_manifest.py (all production entry points, dynamic-import detection)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def rt():
    return _load("run_tests_impact_guard", "scripts/run_tests.py")


@pytest.fixture(scope="module")
def rim():
    return _load("runtime_import_manifest_under_test", "scripts/runtime_import_manifest.py")


@pytest.fixture(scope="module")
def manifest(rim):
    return rim.build_manifest()


def _flat(plan):
    return [t for group in plan for t in group]


# ------------------------------------------------------------------------------------------------ (a) live alpha: demo tests + safety
@pytest.mark.parametrize(
    "path",
    [
        "src/alpha/families/orb.py",
        "src/alpha/families/spec.py",
        "src/alpha/fast/sim.py",
        "src/alpha/fast/store.py",
        "src/alpha/common/market_data.py",
        "src/alpha/common/market_costs.py",
        "src/alpha/session.py",
        "src/alpha/context/__init__.py",
        "src/alpha/timeframe/__init__.py",
        "src/alpha/regime/__init__.py",
        "src/alpha/signals/candidate.py",
        "src/alpha/__init__.py",
    ],
)
def test_live_alpha_paths_pull_demo_tests_and_safety(rt, path):
    plan = rt.plan_for([path])
    assert "tests/unit/demo" in _flat(plan)
    assert ["-m", "safety"] in plan


def test_alpha_common_also_pulls_markets_tests(rt):
    assert "tests/unit/markets" in _flat(rt.plan_for(["src/alpha/common/market_costs.py"]))


def test_research_only_alpha_path_stays_demo_and_safety_free(rt):
    plan = rt.plan_for(["src/alpha/discovery/evaluate.py"])
    assert "tests/unit/alpha" in _flat(plan)
    assert "tests/unit/demo" not in _flat(plan)
    assert ["-m", "safety"] not in plan


# ------------------------------------------------------------------------------------------------ (b) safety-critical production paths
@pytest.mark.parametrize(
    "path",
    [
        "src/exits/engine.py",
        "src/risk/sizing.py",
        "src/execution/orders.py",
        "src/nautilus_mt5/reconciliation.py",
        "src/adapters/activtrades_mt5/real_client.py",
        "src/persistence/store.py",
        "src/demo/runner.py",
        "src/demo/execution/exit_manager.py",
        "src/demo/opportunity/policy.py",
        "src/demo/exit_policies.py",
        "src/data/historical.py",
        "src/markets/spec.py",
        "src/instruments/models.py",
        "src/margin/estimator.py",
        "src/market_observer/observer.py",
        "scripts/autostart/supervisor.py",
        "scripts/autostart/eod_recovery.py",
        "scripts/autostart/deploy_gate.py",
        "scripts/autostart/instance_lock.py",
        "scripts/demo_trader.py",
    ],
)
def test_safety_critical_production_paths_select_the_safety_overlay(rt, path):
    assert ["-m", "safety"] in rt.plan_for([path])


def test_rules_never_narrow_what_the_matrix_selected(rt):
    """The union only adds: every target the pre-existing MATRIX rule gives is still selected for a path in that area."""
    for prefix, targets in rt.MATRIX:
        if prefix == "tests/":
            continue
        flat = _flat(rt.plan_for([prefix + "some_module.py"]))
        for t in targets:
            if "*" in t or t.startswith("m:"):
                continue
            assert t in flat, (prefix, t)


# ------------------------------------------------------------------------------------------------ (c) git failure is loud
def test_git_failure_is_loud_not_an_empty_plan(rt, monkeypatch):
    class _Res:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad revision"

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Res())
    with pytest.raises(RuntimeError, match="git diff --name-only"):
        rt.changed_paths("main")


def test_git_ls_files_failure_is_loud_too(rt, monkeypatch):
    class _Ok:
        returncode = 0
        stdout = "src/risk/policy.py\n"
        stderr = ""

    class _Bad:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def fake(cmd, *a, **k):
        return _Bad() if cmd[1] == "ls-files" else _Ok()

    monkeypatch.setattr(rt.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="ls-files"):
        rt.changed_paths("main")


def test_successful_git_still_returns_sorted_unique_paths(rt, monkeypatch):
    class _Ok:
        returncode = 0
        stdout = "b.py\na.py\n"
        stderr = ""

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: _Ok())
    assert rt.changed_paths("main") == ["a.py", "b.py"]


# ------------------------------------------------------------------------------------------------ (d) unknown paths widen, (e) docs-only
@pytest.mark.parametrize(
    "path", ["totally_new_dir/thing.py", "src/brand_new_pkg/mod.py", "Makefile"]
)
def test_unknown_non_doc_paths_widen_to_more_tests(rt, path):
    plan = rt.plan_for([path])
    assert ["-m", "fast or integration or safety"] in plan
    assert path in rt.impact_plan([path]).widened


def test_docs_only_selects_no_pytest(rt):
    assert rt.plan_for(["docs/ARCHITECTURE.md", "README.md", "reports/x.txt"]) == []
    assert rt.impact_plan(["docs/ARCHITECTURE.md"]).docs_only
    assert not rt.impact_plan(["docs/a.md", "src/risk/policy.py"]).docs_only


# ------------------------------------------------------------------------------------------------ the manifest drives the drift guards
def test_manifest_finds_the_live_closure(manifest):
    assert len(manifest["entry_points"]) >= 5
    assert manifest["totals"]["reachable_src_files"] > 100
    assert manifest["missing_entry_points"] == [] and manifest["unlisted_autostart_files"] == []
    assert {"file", "loc", "package"} <= set(manifest["modules"][0])
    assert [m["file"] for m in manifest["modules"]] == sorted(
        m["file"] for m in manifest["modules"]
    )
    assert manifest["generated_commit"]
    assert (
        manifest["packages"]["alpha"]["reachable_loc"] > 0
        and manifest["packages"]["alpha"]["unreachable_loc"] > 0
    )


def test_every_production_reachable_alpha_file_pulls_demo_tests_and_safety(rt, rim):
    live = rim.reachable_alpha_files()
    assert len(live) > 20  # sanity: the closure really found the live alpha modules
    missing_demo = [p for p in live if "tests/unit/demo" not in _flat(rt.plan_for([p]))]
    missing_safety = [p for p in live if ["-m", "safety"] not in rt.plan_for([p])]
    assert not missing_demo, (
        f"live-reachable alpha files that do not pull tests/unit/demo: {missing_demo}"
    )
    assert not missing_safety, (
        f"live-reachable alpha files without the safety overlay: {missing_safety}"
    )


def test_every_production_reachable_file_selects_the_safety_overlay(rt, rim):
    missing = [p for p in rim.reachable_files() if ["-m", "safety"] not in rt.plan_for([p])]
    assert not missing, (
        f"production-reachable files whose change does not select -m safety: {missing}"
    )


def test_manifest_check_passes_on_the_repo(rim):
    assert rim.check(rim.build_manifest()) == []


def test_entry_points_are_listed_explicitly(rim):
    assert set(rim.ENTRY_POINTS) == {
        "scripts/demo_trader.py",
        "scripts/autostart/supervisor.py",
        "scripts/autostart/eod_recovery.py",
        "scripts/autostart/deploy_gate.py",
        "scripts/autostart/instance_lock.py",
    }
    for e in rim.ENTRY_POINTS:
        assert (REPO / e).is_file(), e
    assert not set(rim.ENTRY_POINTS) & set(rim.EXCLUDED_AUTOSTART)


def test_every_autostart_python_file_is_an_entry_point_or_explicitly_excluded(rim):
    files = rim.autostart_python_files()
    assert files, "no scripts/autostart/*.py found"
    unlisted = [f for f in files if f not in rim.ENTRY_POINTS and f not in rim.EXCLUDED_AUTOSTART]
    assert not unlisted, (
        f"new autostart script(s) not listed in ENTRY_POINTS / EXCLUDED_AUTOSTART of scripts/runtime_import_manifest.py: {unlisted}"
    )


def test_launcher_named_python_files_are_entry_points(rim):
    launched = rim.launcher_python_entry_points()
    assert "scripts/autostart/supervisor.py" in launched and "scripts/demo_trader.py" in launched
    assert [
        p for p in launched if p not in rim.ENTRY_POINTS and p not in rim.EXCLUDED_AUTOSTART
    ] == []


def test_a_new_unlisted_autostart_script_fails_the_guard(rim, tmp_path):
    auto = tmp_path / "scripts" / "autostart"
    auto.mkdir(parents=True)
    (auto / "supervisor.py").write_text("x = 1\n")
    (auto / "brand_new_tool.py").write_text("x = 1\n")
    (auto / "run_new.ps1").write_text(
        "uv run python scripts\\autostart\\launched_but_unlisted.py\n"
    )
    bad = rim.unlisted_autostart_files(tmp_path, entries=("scripts/autostart/supervisor.py",))
    assert bad == [
        "scripts/autostart/brand_new_tool.py",
        "scripts/autostart/launched_but_unlisted.py",
    ]


# ------------------------------------------------------------------------------------------------ closure / dynamic import detection on a synthetic tree
def _tree(tmp_path, entry_src, **mods):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "autostart").mkdir()
    (tmp_path / "scripts" / "e.py").write_text(entry_src)
    pkg = tmp_path / "src" / "pk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for name, body in mods.items():
        (pkg / f"{name}.py").write_text(body)
    return tmp_path


def test_closure_follows_lazy_relative_submodule_and_constant_dynamic_imports(rim, tmp_path):
    root = _tree(
        tmp_path,
        "from pk import a\n",
        a="from . import b\n\n\ndef f():\n    import pk.c\n    import importlib\n    return importlib.import_module('pk.d')\n",
        b="x = 1\n",
        c="y = 1\n",
        d="z = 1\n",
        e="never = 1\n",
    )
    files, sites, missing = rim.compute_closure(["scripts/e.py"], root)
    rel = {p.relative_to(root).as_posix() for p in files}
    assert {"src/pk/__init__.py", "src/pk/a.py", "src/pk/b.py", "src/pk/c.py", "src/pk/d.py"} <= rel
    assert "src/pk/e.py" not in rel and missing == []
    assert [s["target"] for s in sites if s["resolved"]] == ["pk.d"]


def test_unresolved_dynamic_import_is_listed_and_fails_check(rim, tmp_path, monkeypatch):
    root = _tree(
        tmp_path,
        "from pk import a\n",
        a="import importlib\n\n\ndef f(n):\n    return importlib.import_module(n)\n\n\ndef g(p):\n    return __import__(p)\n",
    )
    monkeypatch.setattr(rim, "ENTRY_POINTS", ("scripts/e.py",))
    m = rim.build_manifest(root)
    unresolved = [s for s in m["dynamic_import_sites"] if not s["resolved"]]
    assert {s["callee"] for s in unresolved} == {"import_module", "__import__"} and all(
        s["line"] > 0 for s in unresolved
    )
    problems = rim.check(m)
    assert len(problems) == 2 and all("unresolved dynamic import" in p for p in problems)


def test_missing_entry_point_fails_check(rim, tmp_path, monkeypatch):
    root = _tree(tmp_path, "x = 1\n")
    monkeypatch.setattr(rim, "ENTRY_POINTS", ("scripts/e.py", "scripts/gone.py"))
    m = rim.build_manifest(root)
    assert m["missing_entry_points"] == ["scripts/gone.py"]
    assert any("scripts/gone.py" in p for p in rim.check(m))


def test_cli_writes_json_and_check_returns_zero(rim, tmp_path):
    import json

    out = tmp_path / "artifacts" / "research" / "m.json"
    assert rim.main(["--check", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert {
        "entry_points",
        "modules",
        "packages",
        "dynamic_import_sites",
        "generated_commit",
    } <= set(data)


# ------------------------------------------------------------------------------------------------ tier classification of the observer parity suite
def test_observer_parity_suite_is_slow_tier_and_tiers_still_partition():
    spec = importlib.util.spec_from_file_location(
        "tests_conftest_under_test", REPO / "tests" / "conftest.py"
    )
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)  # type: ignore[union-attr]
    parity = "tests/unit/demo/test_observer_parity.py"
    assert (REPO / parity).is_file()
    assert parity in cf.SLOW_FILES
    # the other observer suites are NOT heavy by evidence (pure unit / fake-stack) and keep their tier
    assert "tests/unit/demo/test_observer_store.py" not in cf.SLOW_FILES
    # every SLOW_FILES entry exists (a typo would silently leave the file in the fast tier)
    assert [f for f in cf.SLOW_FILES if not (REPO / f).is_file()] == []


# ------------------------------------------------------------------------------------------------ files moved out of FAST stay selected
_BASELINE_NOT_FAST = (  # slow + integration entries (dirs, files, prefixes) of tests/conftest.py at the lane base cdc8a56
    "tests/chaos/",
    "tests/replay/",
    "tests/parity/",
    "tests/unit/demo/opportunity/",
    "tests/unit/demo/learning/",
    "tests/temporal/test_real_events_causality.py",
    "tests/temporal/test_prefix_cache_equivalence.py",
    "tests/temporal/test_kernel_reference_parity.py",
    "tests/temporal/test_spec_validate.py",
    "tests/events/test_event_prefix_equality.py",
    "tests/test_ar2_fast_runner.py",
    "tests/test_v2_probe_runner.py",
    "tests/test_alpha_fast_kernels_a.py",
    "tests/test_alpha_fast_kernels_c.py",
    "tests/test_v2_families_planted.py",
    "tests/test_discovery_stages.py",
    "tests/test_ad1_benchmark.py",
    "tests/test_v2_rawscan_core.py",
    "tests/test_alpha_fast_sim_target_guard.py",
    "tests/test_alpha_fast_screen.py",
    "tests/test_v2_probe_null.py",
    "tests/test_temporal_discovery_search.py",
    "tests/test_v2_multimarket_loader.py",
    "tests/test_alpha_fast_price_action.py",
    "tests/test_formula_alpha_gp.py",
    "tests/test_v2_metalabel_eval.py",
    "tests/test_formula_alpha_real.py",
    "tests/test_v2_probe_clock.py",
    "tests/test_v2_directional_context.py",
    "tests/test_v2_multimarket_frame.py",
    "tests/test_discovery_grammar.py",
    "tests/test_temporal_discovery_genome.py",
    "tests/test_temporal_discovery_evaluate.py",
    "tests/test_v2_session_levels.py",
    "tests/integration/",
    "tests/unit/demo/runner/",
    "tests/unit/demo/store/",
    "tests/unit/demo/execution/test_live_stack.py",
    "tests/unit/nautilus_mt5/",
    "tests/unit/nautilus_kernel/",
    "tests/unit/adapters/",
    "tests/unit/persistence/",
    "tests/unit/scripts/",
    "tests/unit/research/test_backtest_cli.py",
    "tests/events/",
    "tests/temporal/test_real_events_frame.py",
)


# test files that did NOT exist at the baseline: they were never part of the FAST tier, so placing them in slow/integration
# is not a "move" and needs no MOVED_OUT_OF_FAST entry
_NEW_NON_FAST_TESTS = ("tests/unit/research_workbench/test_differential_e2e.py",)


def _conftest():
    spec = importlib.util.spec_from_file_location(
        "tests_conftest_moved_guard", REPO / "tests" / "conftest.py"
    )
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)  # type: ignore[union-attr]
    return cf


def test_every_file_moved_out_of_fast_is_listed_in_moved_out_of_fast(rt):
    """A test file that was FAST at the baseline and is slow/integration now must be named in impact_tests.MOVED_OUT_OF_FAST, because
    `-m fast`-based plans (unknown-path widening) would otherwise silently stop running it."""
    cf = _conftest()
    now_not_fast = (*cf.SLOW_DIRS, *cf.SLOW_FILES, *cf.INTEGRATION_PREFIXES)
    moved = []
    for p in sorted((REPO / "tests").rglob("test_*.py")):
        rel = p.relative_to(REPO).as_posix()
        if (
            rel.startswith(now_not_fast)
            and not rel.startswith(_BASELINE_NOT_FAST)
            and rel not in _NEW_NON_FAST_TESTS
        ):
            moved.append(rel)
    missing = [m for m in moved if m not in rt.impact_tests.MOVED_OUT_OF_FAST]
    assert not missing, (
        f"moved out of FAST in tests/conftest.py but not in MOVED_OUT_OF_FAST: {missing}"
    )
    assert (
        "tests/unit/demo/test_observer_parity.py" in moved
    )  # the guard really detects the known move
    assert all((REPO / m).is_file() for m in rt.impact_tests.MOVED_OUT_OF_FAST)


@pytest.mark.parametrize(
    "path", ["totally_new_dir/thing.py", "src/brand_new_pkg/mod.py", "src/unmapped_domain/x.py"]
)
def test_fast_marker_plans_also_select_the_moved_files_by_path(rt, path):
    moved = "tests/unit/demo/test_observer_parity.py"
    assert moved in _flat(rt.plan_for([path]))
    assert moved in rt.impact_plan([path]).targets


def test_changed_plan_for_unknown_paths_selects_the_moved_file(rt, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["run_tests.py", "changed", "src/brand_new_pkg/mod.py", "--dry-run"]
    )
    assert rt.main() == 0
    out = capsys.readouterr().out
    assert (
        "tests/unit/demo/test_observer_parity.py" in out
        and "-m fast or integration or safety" in out
    )


def test_plans_without_a_fast_mark_do_not_add_the_moved_file(rt):
    assert "tests/unit/demo/test_observer_parity.py" not in _flat(
        rt.plan_for(["src/risk/policy.py"])
    )


# ------------------------------------------------------------------------------------------------ reviewed dynamic sites + launcher table
def test_reviewed_dynamic_site_is_keyed_on_its_arguments(rim, tmp_path, monkeypatch):
    root = _tree(
        tmp_path,
        "from pk import a\n",
        a="import importlib\n\n\ndef f(n):\n    return importlib.import_module(n)\n",
    )
    monkeypatch.setattr(rim, "ENTRY_POINTS", ("scripts/e.py",))
    reviewed = {("src/pk/a.py", "import_module", "n"): "reviewed: n is a literal-checked name"}
    monkeypatch.setattr(rim, "REVIEWED_DYNAMIC_SITES", reviewed)
    assert rim.check(rim.build_manifest(root)) == []  # the reviewed site stays accepted
    (root / "src" / "pk" / "a.py").write_text(
        "import importlib\n\n\ndef f(n):\n    return importlib.import_module(n)\n\n\ndef g(m):\n    return importlib.import_module(m)\n"
    )
    problems = rim.check(rim.build_manifest(root))
    assert len(problems) == 1 and "import_module(m)" in problems[0]


def test_launcher_table_covers_every_launcher_file_and_matches_the_regex(rim):
    assert rim.launcher_table_problems() == []
    assert set(rim.launcher_files()) == set(rim.LAUNCHERS)


def test_launcher_table_drift_fails(rim, tmp_path):
    auto = tmp_path / "scripts" / "autostart"
    auto.mkdir(parents=True)
    (auto / "new_launcher.ps1").write_text("Write-Host hi\n")
    (auto / "indirect.ps1").write_text("uv run python scripts/autostart/supervisor.py\n")
    (auto / "listed.ps1").write_text("Write-Host hi\n")
    table = {
        "scripts/autostart/indirect.ps1": ([], "starts no python"),
        "scripts/autostart/listed.ps1": ([], ""),
        "scripts/autostart/ghost.ps1": ([], "x"),
    }
    problems = rim.launcher_table_problems(tmp_path, table)
    joined = "\n".join(problems)
    assert "launcher not in LAUNCHERS table: scripts/autostart/new_launcher.ps1" in joined
    assert (
        "regex finds python entry point scripts/autostart/supervisor.py in scripts/autostart/indirect.ps1"
        in joined
    )
    assert "gives no reason: scripts/autostart/listed.ps1" in joined
    assert "row without a file: scripts/autostart/ghost.ps1" in joined
