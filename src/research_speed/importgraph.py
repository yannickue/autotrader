# ruff: noqa: E501
"""Static import closure and content hashing of exactly the code a result depends on.

The closure is an OVER-approximation (every ``import`` statement anywhere in a file counts, also inside functions or
``if`` branches), so a change to any module that COULD influence a result changes the hash. Dynamic imports
(``importlib``, ``__import__``) are not followed; modules that are loaded that way must be listed explicitly as entries.

The hash is a content hash (sha256 of the file bytes with CRLF normalised to LF, i.e. what git stores for a text file
with ``core.autocrlf``). Modification times are never used. Uncommitted edits count (the working-tree content is hashed).
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path


def content_digest(path: Path) -> str:
    """sha256 of the file content with CRLF -> LF (line-ending independent)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _module_file(module: str, roots: Sequence[Path]) -> Path | None:
    parts = module.split(".")
    for root in roots:
        base = root.joinpath(*parts)
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
    return None


def _package_of(path: Path, roots: Sequence[Path]) -> list[str] | None:
    for root in roots:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if path.name == "__init__.py":
            return parts[:-1]
        return parts[:-1]
    return None


@lru_cache(maxsize=4096)
def _imports_of(path_str: str, roots_key: tuple[str, ...]) -> tuple[str, ...]:
    path = Path(path_str)
    roots = tuple(Path(r) for r in roots_key)
    try:
        tree = ast.parse(path.read_bytes().replace(b"\r\n", b"\n"))
    except (SyntaxError, ValueError):
        return ()
    pkg = _package_of(path, roots)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if pkg is None:
                    continue
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join([*base, *(node.module.split(".") if node.module else [])])
            else:
                mod = node.module or ""
            if not mod:
                continue
            found.add(mod)
            for a in node.names:
                if a.name != "*":
                    found.add(f"{mod}.{a.name}")
    return tuple(sorted(found))


def closure(
    entries: Iterable[Path], roots: Sequence[Path], exclude: Iterable[Path] = ()
) -> list[Path]:
    """All repo files reachable from ``entries`` through imports that resolve below ``roots`` (sorted, de-duplicated).

    Parent packages' ``__init__.py`` of every resolved module are included (they execute on import). Files in ``exclude`` are
    neither included nor traversed (use for code that provably does not influence the result, e.g. the control selection when
    fingerprinting the events step).
    """
    roots_key = tuple(str(Path(r).resolve()) for r in roots)
    root_paths = [Path(r) for r in roots_key]
    skip = {Path(e).resolve() for e in exclude}
    seen: dict[Path, None] = {}
    stack = [Path(e).resolve() for e in entries]
    while stack:
        p = stack.pop()
        if p in seen or p in skip or not p.is_file():
            continue
        seen[p] = None
        for mod in _imports_of(str(p), roots_key):
            parts = mod.split(".")
            for k in range(1, len(parts) + 1):
                f = _module_file(".".join(parts[:k]), root_paths)
                if f is not None:
                    stack.append(f.resolve())
    return sorted(seen)


_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module", "spec_from_file_location", "spec_from_loader", "exec_module", "run_path", "run_module"})


def dynamic_import_files(files: Iterable[Path]) -> list[Path]:
    """Files that load code dynamically (``importlib.import_module``, ``__import__``, importlib.util spec loading, runpy): the static closure cannot see
    what they load, so a result that depends on them must never be served from a cache keyed by the static closure."""
    out = []
    for f in files:
        try:
            tree = ast.parse(Path(f).read_bytes().replace(b"\r\n", b"\n"))
        except (SyntaxError, ValueError, OSError):
            out.append(Path(f))  # unparsable => unknown => treat as dynamic
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in _DYNAMIC_IMPORT_NAMES:
                    out.append(Path(f))
                    break
    return sorted(set(out))


def hash_files(files: Iterable[Path], base: Path) -> str:
    """Order-independent hash over (posix relative path, content digest) pairs."""
    base = base.resolve()
    h = hashlib.sha256()
    rows = []
    for f in files:
        f = Path(f).resolve()
        try:
            rel = f.relative_to(base).as_posix()
        except ValueError:
            rel = f.as_posix()
        rows.append((rel, content_digest(f)))
    for rel, dig in sorted(rows):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(dig.encode())
        h.update(b"\n")
    return h.hexdigest()


def code_hash(
    entries: Iterable[Path], roots: Sequence[Path], base: Path, exclude: Iterable[Path] = ()
) -> str:
    """Content hash of the import closure of ``entries``."""
    return hash_files(closure(entries, roots, exclude), base)


def tree_hash(
    directory: Path, base: Path, suffixes: Sequence[str] = (".toml", ".yaml", ".yml", ".json")
) -> str:
    """Content hash of all data/config files of a directory tree (configs, fixtures)."""
    files = [p for p in sorted(directory.rglob("*")) if p.is_file() and p.suffix in suffixes]
    return hash_files(files, base)
