# ruff: noqa: E501
"""Import closure / content hash / artifact fingerprint: what exactly invalidates a cached result."""

from __future__ import annotations

import os
from pathlib import Path

from research_speed import importgraph as IG
from research_speed.artifact import ArtifactFingerprint, config_hash


def _pkg(root: Path) -> dict[str, Path]:
    src = root / "src"
    (src / "pkg" / "sub").mkdir(parents=True)
    (src / "other").mkdir()
    files = {
        "pkg_init": src / "pkg" / "__init__.py",
        "main": src / "pkg" / "main.py",
        "helper": src / "pkg" / "helper.py",
        "sub_init": src / "pkg" / "sub" / "__init__.py",
        "deep": src / "pkg" / "sub" / "deep.py",
        "lazy": src / "other" / "lazy.py",
        "other_init": src / "other" / "__init__.py",
        "unrelated": src / "other" / "unrelated.py",
    }
    files["pkg_init"].write_text("")
    files["sub_init"].write_text("")
    files["other_init"].write_text("")
    files["main"].write_text(
        "import json\nfrom . import helper\n\n\ndef f():\n    from other.lazy import g  # function-level import must count\n    return g\n"
    )
    files["helper"].write_text("from .sub.deep import x\n")
    files["deep"].write_text("x = 1\n")
    files["lazy"].write_text("def g():\n    return 2\n")
    files["unrelated"].write_text("y = 3\n")
    return files


def _names(paths: list[Path], root: Path) -> set[str]:
    return {p.relative_to(root / "src").as_posix() for p in paths}


def test_closure_follows_relative_lazy_and_package_imports_but_not_unrelated_files(tmp_path):
    f = _pkg(tmp_path)
    names = _names(IG.closure([f["main"]], [tmp_path / "src"]), tmp_path)
    expected = {
        "pkg/main.py",
        "pkg/helper.py",
        "pkg/sub/deep.py",
        "other/lazy.py",
        "pkg/__init__.py",
        "pkg/sub/__init__.py",
        "other/__init__.py",
    }
    assert expected <= names
    assert "other/unrelated.py" not in names


def test_exclude_neither_includes_nor_traverses(tmp_path):
    f = _pkg(tmp_path)
    names = _names(IG.closure([f["main"]], [tmp_path / "src"], exclude=[f["helper"]]), tmp_path)
    assert (
        "pkg/helper.py" not in names and "pkg/sub/deep.py" not in names and "other/lazy.py" in names
    )


def test_code_hash_follows_content_not_mtime_or_line_endings(tmp_path):
    f = _pkg(tmp_path)
    roots = [tmp_path / "src"]
    h0 = IG.code_hash([f["main"]], roots, tmp_path)
    os.utime(f["deep"], (1, 1))  # mtime change: no effect
    assert IG.code_hash([f["main"]], roots, tmp_path) == h0
    f["deep"].write_bytes(b"x = 1\r\n")  # CRLF vs LF: no effect
    assert IG.code_hash([f["main"]], roots, tmp_path) == h0
    f["deep"].write_text("x = 2\n")  # a real content change in a TRANSITIVE dependency
    h1 = IG.code_hash([f["main"]], roots, tmp_path)
    assert h1 != h0
    f["unrelated"].write_text("y = 99\n")  # not imported: no effect
    assert IG.code_hash([f["main"]], roots, tmp_path) == h1


def test_artifact_id_is_stable_and_every_component_changes_it():
    base = ArtifactFingerprint("d", "f", "c", "cfg", "lab", "pre")
    assert base.artifact_id == ArtifactFingerprint("d", "f", "c", "cfg", "lab", "pre").artifact_id
    for name in base.components():
        other = ArtifactFingerprint(**{**base.components(), name: "CHANGED"})
        assert other.artifact_id != base.artifact_id
        assert base.diff(other) == [name]
    assert base.diff(base) == []


def test_config_hash_is_order_independent_and_value_sensitive():
    assert config_hash({"a": 1, "b": [1, 2]}) == config_hash({"b": [1, 2], "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})
