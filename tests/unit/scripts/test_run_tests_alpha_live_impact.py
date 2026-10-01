"""Impact matrix: production-reachable alpha modules must also pull the demo tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "run_tests_under_test", _ROOT / "scripts" / "run_tests.py"
)
assert _spec is not None and _spec.loader is not None
run_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_tests)


def _flat(plan: list[list[str]]) -> list[str]:
    return [t for group in plan for t in group]


@pytest.mark.parametrize(
    "path",
    [
        "src/alpha/families/orb.py",
        "src/alpha/families/spec.py",
        "src/alpha/fast/sim.py",
        "src/alpha/common/market_data.py",
        "src/alpha/common/market_costs.py",
        "src/alpha/session.py",
    ],
)
def test_live_reachable_alpha_paths_pull_demo_tests(path: str) -> None:
    assert "tests/unit/demo" in _flat(run_tests.plan_for([path]))


def test_research_only_alpha_path_stays_demo_free() -> None:
    plan = _flat(run_tests.plan_for(["src/alpha/discovery/evaluate.py"]))
    assert "tests/unit/alpha" in plan
    assert "tests/unit/demo" not in plan


def test_alpha_common_also_pulls_markets_tests() -> None:
    assert "tests/unit/markets" in _flat(run_tests.plan_for(["src/alpha/common/market_costs.py"]))


def test_unmapped_non_doc_path_fails_open_to_fast_tier() -> None:
    plan = run_tests.plan_for(["totally_new_dir/thing.py"])
    assert ["-m", "fast"] in plan


def test_docs_only_change_selects_nothing() -> None:
    assert run_tests.plan_for(["docs/ARCHITECTURE.md", "README.md"]) == []


def test_git_failure_is_loud_not_an_empty_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Res:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad revision"

    monkeypatch.setattr(run_tests.subprocess, "run", lambda *a, **k: _Res())
    with pytest.raises(RuntimeError, match="git diff --name-only"):
        run_tests.changed_paths("main")


def _live_alpha_files() -> list[str]:
    """Static import closure from the production entry points -> reachable src/alpha files."""
    import ast

    src = _ROOT / "src"
    mods: dict[str, Path] = {}
    for p in src.rglob("*.py"):
        name = ".".join(p.relative_to(src).with_suffix("").parts)
        mods[name.removesuffix(".__init__")] = p

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in mods:
                return ".".join(parts[:i])
        return None

    def imported(path: Path, cur: str) -> set[str]:
        out: set[str] = set()
        pkg = cur if path.name == "__init__.py" else cur.rpartition(".")[0]
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            bases: list[str] = []
            if isinstance(n, ast.Import):
                bases = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    keep = pkg.split(".") if pkg else []
                    keep = keep[: len(keep) - (n.level - 1)]
                    base = ".".join([*keep, *([n.module] if n.module else [])])
                else:
                    base = n.module or ""
                if base:
                    bases = [base, *[f"{base}.{a.name}" for a in n.names]]
            out |= {r for b in bases if (r := resolve(b))}
        return out

    entries = [_ROOT / "scripts" / "demo_trader.py"]
    entries += [_ROOT / "scripts" / "autostart" / f for f in ("supervisor.py", "eod_recovery.py")]
    todo: set[str] = set()
    for e in entries:
        todo |= imported(e, "__main__")
    seen: set[str] = set()
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        parts = m.split(".")
        todo |= {".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in mods}
        todo |= imported(mods[m], m)
    return sorted(mods[m].relative_to(_ROOT).as_posix() for m in seen if m.split(".")[0] == "alpha")


def test_every_production_reachable_alpha_file_pulls_the_demo_tests() -> None:
    live = _live_alpha_files()
    assert len(live) > 20  # sanity: the closure really found the live alpha modules
    missing = [p for p in live if "tests/unit/demo" not in _flat(run_tests.plan_for([p]))]
    assert not missing, f"live-reachable alpha files that do not pull tests/unit/demo: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "src/exits/engine.py",
        "src/risk/sizing.py",
        "src/execution/orders.py",
        "src/nautilus_mt5/reconciliation.py",
        "src/adapters/activtrades_mt5/real_client.py",
        "src/persistence/store.py",
        "src/demo/execution/exit_manager.py",
        "src/demo/runner.py",
        "scripts/autostart/supervisor.py",
        "scripts/autostart/eod_recovery.py",
    ],
)
def test_safety_critical_paths_select_the_safety_overlay(path: str) -> None:
    assert ["-m", "safety"] in run_tests.plan_for([path])


def test_research_only_alpha_path_does_not_select_safety_overlay() -> None:
    assert ["-m", "safety"] not in run_tests.plan_for(["src/alpha/discovery/evaluate.py"])


def test_every_production_reachable_alpha_file_selects_the_safety_overlay() -> None:
    missing = [p for p in _live_alpha_files() if ["-m", "safety"] not in run_tests.plan_for([p])]
    assert not missing, f"live alpha files without the safety overlay: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "src/data/historical.py",
        "src/markets/spec.py",
        "src/instruments/models.py",
        "src/margin/estimator.py",
        "src/demo/opportunity/policy.py",
        "src/demo/exit_policies.py",
    ],
)
def test_other_production_reachable_domains_select_the_safety_overlay(path: str) -> None:
    assert ["-m", "safety"] in run_tests.plan_for([path])
