# ruff: noqa: E501
"""Production runtime import manifest (tooling only; never imported by production code).

Computes the static import closure over ``src/`` (and the sibling ``scripts/`` helpers) that is reachable from the PRODUCTION entry
points and writes it as machine-readable JSON. It is the single source of truth for "which files can run in production", used by the
impact-matrix drift-guard tests (``tests/unit/scripts/test_impact_live_alpha_and_safety.py``) and by ``docs/TEST_GATES.md``.

Entry points
  * ``scripts/demo_trader.py`` and ``scripts/autostart/{supervisor,eod_recovery,deploy_gate,instance_lock}.py`` (explicit list below);
  * every ``scripts/**/*.py`` that a ``scripts/autostart/*.ps1`` / ``*.task.xml`` launcher names (parsed, must be in the explicit list).

Closure
  Re-uses ``src/research_speed/importgraph.py`` (AST; every ``import`` anywhere in a file counts, also lazy/function-level ones;
  relative imports resolved; ``from x import y`` submodules and parent packages' ``__init__`` included). A string-constant
  ``importlib.import_module("pkg.mod")`` / ``__import__("pkg.mod")`` is resolved and followed (to a fixed point). Everything else that
  loads code dynamically is listed under ``dynamic_import_sites`` with ``resolved: false`` and fails ``--check`` unless it is in the
  reviewed ``REVIEWED_DYNAMIC_SITES`` table (each with a reason) - a new, unreviewed dynamic import can therefore never silently
  shrink the impact matrix.

Usage:
    python scripts/runtime_import_manifest.py [--out PATH] [--check] [--summary]
Default output: ``artifacts/research/runtime_import_manifest.json`` (directory created; artifacts are never committed).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
AUTOSTART = SCRIPTS / "autostart"
DEFAULT_OUT = ROOT / "artifacts" / "research" / "runtime_import_manifest.json"

# the production entry points (repo-relative posix paths); keep in sync with scripts/autostart/*.ps1 (a drift test enforces it)
ENTRY_POINTS: tuple[str, ...] = (
    "scripts/demo_trader.py",
    "scripts/autostart/supervisor.py",
    "scripts/autostart/eod_recovery.py",
    "scripts/autostart/deploy_gate.py",
    "scripts/autostart/instance_lock.py",
)
# scripts/autostart/*.py that are deliberately NOT production entry points (operator tools) - with the reason
EXCLUDED_AUTOSTART: dict[str, str] = {
    "scripts/autostart/approve_deploy.py": "interactive operator tool that writes the deploy approval; never launched by the scheduler",
}
# reviewed dynamic-import sites that cannot be resolved statically: (repo-relative file, callee) -> reason. Empty unless reviewed.
REVIEWED_DYNAMIC_SITES: dict[tuple[str, str], str] = {}

_IMPORT_CALLEES = frozenset({"import_module", "__import__"})
_LOADER_CALLEES = frozenset(
    {"spec_from_file_location", "spec_from_loader", "exec_module", "run_path", "run_module"}
)
_EXEC_CALLEES = frozenset({"exec", "eval"})
_PY_IN_LAUNCHER = re.compile(r"((?:scripts[\\/])(?:[\w.-]+[\\/])*[\w.-]+\.py)", re.IGNORECASE)


def _posix(p: Path, root: Path = ROOT) -> str:
    return p.resolve().relative_to(root.resolve()).as_posix()


def _importgraph():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from research_speed import importgraph

    return importgraph


def launcher_python_entry_points(root: Path = ROOT) -> list[str]:
    """Python files named by ``scripts/autostart/*.ps1`` / ``*.task.xml`` launchers (repo-relative posix, sorted)."""
    out: set[str] = set()
    auto = root / "scripts" / "autostart"
    for f in [*auto.glob("*.ps1"), *auto.glob("*.xml")]:
        for m in _PY_IN_LAUNCHER.findall(f.read_text(encoding="utf-8", errors="replace")):
            out.add(m.replace("\\", "/"))
    return sorted(out)


def autostart_python_files(root: Path = ROOT) -> list[str]:
    return sorted(_posix(p, root) for p in (root / "scripts" / "autostart").glob("*.py"))


def unlisted_autostart_files(
    root: Path = ROOT, entries: tuple[str, ...] | None = None
) -> list[str]:
    """``scripts/autostart/*.py`` files that are neither a listed entry point nor explicitly excluded, plus python files named by
    a launcher (``*.ps1`` / ``*.task.xml``) that are not listed entry points: each is a drift the guard must fail on."""
    listed = set(ENTRY_POINTS if entries is None else entries)
    known = listed | set(EXCLUDED_AUTOSTART)
    bad = [p for p in autostart_python_files(root) if p not in known]
    bad += [
        p
        for p in launcher_python_entry_points(root)
        if p not in listed and p not in EXCLUDED_AUTOSTART
    ]
    return sorted(set(bad))


def _callee(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _const_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def dynamic_sites_in(path: Path, root: Path = ROOT) -> list[dict[str, Any]]:
    """Dynamic code-loading call sites of one file: ``importlib.import_module``, ``__import__``, importlib.util spec loading, runpy,
    bare ``exec``/``eval`` (their string argument may contain imports). ``resolved`` + ``target`` when the module name is a constant."""
    rel = _posix(path, root)
    try:
        tree = ast.parse(path.read_bytes().replace(b"\r\n", b"\n"))
    except (SyntaxError, ValueError, OSError):
        return [
            {"file": rel, "line": 0, "callee": "<unparsable>", "resolved": False, "target": None}
        ]
    sites: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        if name is None:
            continue
        is_bare = isinstance(node.func, ast.Name)
        if name in _IMPORT_CALLEES:
            target = _const_str(node.args[0]) if node.args else None
            resolved = target is not None and not target.startswith(".")
        elif name in _LOADER_CALLEES:
            target, resolved = None, False
        elif name in _EXEC_CALLEES and is_bare:
            arg = _const_str(node.args[0]) if node.args else None
            target, resolved = None, arg is not None and "import" not in arg
        else:
            continue
        sites.append(
            {
                "file": rel,
                "line": node.lineno,
                "callee": name,
                "resolved": bool(resolved),
                "target": target if resolved else None,
            }
        )
    return sorted(sites, key=lambda s: (s["file"], s["line"], s["callee"]))


def _roots() -> list[Path]:
    # src first (production packages), then the scripts directories so sibling helpers (``import instance_lock``) resolve
    return [SRC, SCRIPTS, AUTOSTART]


def compute_closure(
    entry_points: list[str] | None = None, root: Path = ROOT
) -> tuple[list[Path], list[dict[str, Any]], list[str]]:
    """(closure files, dynamic sites inside the closure, missing entry points). Fixed point over resolved dynamic imports."""
    ig = _importgraph()
    entries = list(entry_points if entry_points is not None else ENTRY_POINTS)
    missing = [e for e in entries if not (root / e).is_file()]
    present = [root / e for e in entries if (root / e).is_file()]
    roots = [root / "src", root / "scripts", root / "scripts" / "autostart"]
    extra: list[Path] = []
    while True:
        files = ig.closure([*present, *extra], roots)
        sites = [s for f in files for s in dynamic_sites_in(f, root)]
        new: list[Path] = []
        for s in sites:
            if s["resolved"] and s["target"]:
                for k in range(1, len(s["target"].split(".")) + 1):
                    f = ig._module_file(".".join(s["target"].split(".")[:k]), roots)
                    if f is not None and f.resolve() not in {x.resolve() for x in [*files, *extra]}:
                        new.append(f)
        if not new:
            return files, sites, missing
        extra += new


def _loc(path: Path) -> int:
    return len(path.read_bytes().replace(b"\r\n", b"\n").splitlines())


def _git_commit(root: Path = ROOT) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        head = r.stdout.strip() or "UNKNOWN"
        d = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return head + ("+dirty" if d.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def _pkg_of(rel: str, depth: int) -> str:
    parts = rel.split("/")
    if parts[0] != "src":
        return "/".join(parts[:2]) if len(parts) > 1 else parts[0]
    stem = parts[1:]
    if len(stem) <= depth:  # a module directly under the package level
        return stem[0].removesuffix(".py") if len(stem) == 1 else "/".join(stem[:-1])
    return ".".join(stem[:depth])


def build_manifest(root: Path = ROOT) -> dict[str, Any]:
    launcher = launcher_python_entry_points(root)
    entry_points = sorted(set(ENTRY_POINTS) | {p for p in launcher if (root / p).is_file()})
    files, sites, missing = compute_closure(entry_points, root)
    modules = [
        {"file": _posix(f, root), "loc": _loc(f), "package": _pkg_of(_posix(f, root), 1)}
        for f in files
    ]
    modules.sort(key=lambda m: m["file"])
    reach = {m["file"] for m in modules}
    all_src = sorted(p for p in (root / "src").rglob("*.py") if "__pycache__" not in p.parts)

    def summarize(depth: int) -> dict[str, dict[str, int]]:
        agg: dict[str, dict[str, int]] = {}
        for p in all_src:
            rel = _posix(p, root)
            key = _pkg_of(rel, depth)
            row = agg.setdefault(
                key,
                {
                    "reachable_loc": 0,
                    "unreachable_loc": 0,
                    "reachable_files": 0,
                    "unreachable_files": 0,
                },
            )
            kind = "reachable" if rel in reach else "unreachable"
            row[f"{kind}_loc"] += _loc(p)
            row[f"{kind}_files"] += 1
        return dict(sorted(agg.items()))

    reviewed = [
        s for s in sites if not s["resolved"] and (s["file"], s["callee"]) in REVIEWED_DYNAMIC_SITES
    ]
    unresolved = [
        s
        for s in sites
        if not s["resolved"] and (s["file"], s["callee"]) not in REVIEWED_DYNAMIC_SITES
    ]
    for s in sites:
        key = (s["file"], s["callee"])
        s["reviewed_reason"] = REVIEWED_DYNAMIC_SITES.get(key)
    src_mods = [m for m in modules if m["file"].startswith("src/")]
    return {
        "generated_commit": _git_commit(root),
        "entry_points": entry_points,
        "launcher_entry_points": launcher,
        "excluded_autostart": dict(EXCLUDED_AUTOSTART),
        "missing_entry_points": missing,
        "unlisted_autostart_files": unlisted_autostart_files(root),
        "modules": modules,
        "packages": summarize(1),
        "subpackages": summarize(2),
        "totals": {
            "reachable_src_files": len(src_mods),
            "reachable_src_loc": sum(m["loc"] for m in src_mods),
            "reachable_script_files": len(modules) - len(src_mods),
            "total_src_files": len(all_src),
            "total_src_loc": sum(_loc(p) for p in all_src),
        },
        "dynamic_import_sites": sites,
        "unresolved_dynamic_sites": len(unresolved),
        "reviewed_dynamic_sites": len(reviewed),
    }


def reachable_files(root: Path = ROOT) -> list[str]:
    """Repo-relative posix paths of every file in the production closure (src + sibling scripts)."""
    files, _, _ = compute_closure(None, root)
    return sorted(_posix(f, root) for f in files)


def reachable_alpha_files(root: Path = ROOT) -> list[str]:
    return [p for p in reachable_files(root) if p.startswith("src/alpha/")]


def check(manifest: dict[str, Any]) -> list[str]:
    problems = [f"entry point file missing: {m}" for m in manifest["missing_entry_points"]]
    problems += [
        f"autostart python file neither an entry point nor excluded: {p}"
        for p in manifest["unlisted_autostart_files"]
    ]
    for s in manifest["dynamic_import_sites"]:
        if not s["resolved"] and not s["reviewed_reason"]:
            problems.append(f"unresolved dynamic import: {s['file']}:{s['line']} {s['callee']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero on an unresolved dynamic import or a missing entry point",
    )
    ap.add_argument("--summary", action="store_true", help="print a short summary")
    args = ap.parse_args(argv)
    manifest = build_manifest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    t = manifest["totals"]
    print(
        f"manifest -> {args.out}: {t['reachable_src_files']}/{t['total_src_files']} src files reachable "
        f"({t['reachable_src_loc']}/{t['total_src_loc']} LOC), {t['reachable_script_files']} script files, "
        f"{len(manifest['dynamic_import_sites'])} dynamic import sites ({manifest['unresolved_dynamic_sites']} unresolved)"
    )
    problems = check(manifest)
    for p in problems:
        print(f"CHECK: {p}", file=sys.stderr)
    return 1 if args.check and problems else 0


if __name__ == "__main__":
    sys.exit(main())
