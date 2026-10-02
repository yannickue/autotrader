"""Guard: nothing under src/research_workbench is reachable from the production import closure."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _manifest():
    spec = importlib.util.spec_from_file_location(
        "runtime_import_manifest", ROOT / "scripts" / "runtime_import_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["runtime_import_manifest"] = module
    spec.loader.exec_module(module)
    return module


def test_research_workbench_is_not_production_reachable():
    reachable = _manifest().reachable_files(ROOT)
    assert reachable, "the production closure must not be empty (vacuous guard)"
    assert any(p.startswith("src/") for p in reachable)
    leaked = [p for p in reachable if p.startswith("src/research_workbench")]
    assert leaked == [], f"research_workbench reachable from production: {leaked[:5]}"
    assert (ROOT / "src" / "research_workbench").is_dir()
