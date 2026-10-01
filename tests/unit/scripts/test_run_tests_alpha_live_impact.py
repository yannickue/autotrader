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
