"""Drift guard: the alpha boundary docstring must match the production import closure."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import alpha

ROOT = Path(__file__).resolve().parents[3]


def _manifest():
    spec = importlib.util.spec_from_file_location(
        "runtime_import_manifest", ROOT / "scripts" / "runtime_import_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["runtime_import_manifest"] = module
    spec.loader.exec_module(module)
    return module


def _block(name: str) -> list[str]:
    doc = alpha.__doc__ or ""
    match = re.search(rf"{name}_BEGIN\n(.*?)\n{name}_END", doc, re.S)
    assert match, f"{name} block missing from alpha.__doc__"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def test_live_reachable_list_equals_manifest():
    assert sorted(_block("LIVE_REACHABLE")) == sorted(_manifest().reachable_alpha_files(ROOT))


def test_research_only_entries_exist_and_are_not_reachable():
    reachable = set(_manifest().reachable_alpha_files(ROOT))
    for entry in _block("RESEARCH_ONLY"):
        path = ROOT / "src" / "alpha" / entry
        assert path.exists(), entry
        rel = f"src/alpha/{entry}"
        assert not any(
            r == rel or (entry.endswith("/") and r.startswith(rel)) for r in reachable
        ), entry


def test_every_alpha_module_is_classified():
    reachable = set(_manifest().reachable_alpha_files(ROOT))
    research = _block("RESEARCH_ONLY")
    for path in (ROOT / "src" / "alpha").rglob("*.py"):
        rel = path.relative_to(ROOT / "src" / "alpha").as_posix()
        full = f"src/alpha/{rel}"
        in_research = any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in research)
        assert (full in reachable) != in_research, f"{full} unclassified or doubly classified"
