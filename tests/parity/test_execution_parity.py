"""C6: deterministic execution/safety parity between the legacy oracle and the Nautilus path."""

import json
import pathlib

import pytest

from tests.parity.scenarios import SCENARIOS, Category

RESOLVED = {Category.MATCH, Category.EXPECTED_DIFFERENCE, Category.LEGACY_BUG}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.__name__ for s in SCENARIOS])
def test_parity_scenario_has_no_unresolved_or_adapter_divergence(scenario, tmp_path):
    result = scenario(tmp_path)
    assert result.category in RESOLVED, f"{result.name}: {result.category} -- {result.detail}"
    if result.category is Category.LEGACY_BUG:
        assert result.severity in ("Medium", "Low"), (
            "a High/Critical legacy divergence is unresolved"
        )


def test_parity_report_is_deterministic_and_written(tmp_path):
    def run_all(root):
        return [s(root / s.__name__) for s in SCENARIOS]

    first = [(r.name, r.category.value) for r in run_all(tmp_path / "a")]
    second = [(r.name, r.category.value) for r in run_all(tmp_path / "b")]
    assert first == second
    report = [
        {
            "name": r.name,
            "area": r.area,
            "category": r.category.value,
            "severity": r.severity,
            "detail": r.detail,
        }
        for r in run_all(tmp_path / "c")
    ]
    out = pathlib.Path("artifacts") / "parity"
    out.mkdir(parents=True, exist_ok=True)
    (out / "execution_parity.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    bad = [r for r in report if r["category"] in ("UNRESOLVED", "NAUTILUS/ADAPTER_BUG")]
    assert bad == []
