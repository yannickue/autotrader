# ruff: noqa: E501
"""Green-test-result cache (tooling only; DEFAULT OFF, enabled per run with ``run_tests.py changed --result-cache``).

A previous GREEN run of an explicit list of test files may be reused only when EVERYTHING that can influence it is
byte-identical:

* the content of every test file and of the conftest chain above it,
* the content of the static import closure of those files (repo modules under ``src/``, ``scripts/``, test helpers),
* ``pyproject.toml``, ``uv.lock``, the Python version, the pytest argument list,
* ``configs/`` and ``tests/fixtures/`` (data files read at runtime are not visible to an import graph).

In doubt it is a MISS. The cache never serves

* an invocation selected by ``-m`` (no explicit file list),
* a file that is part of the SAFETY overlay, a chaos/replay/parity/broker/serial suite, or the slow / integration tier
  (``tests/conftest.py`` classification is the single source) - so no safety segment is ever "proved" by the cache,
* the segments ``fast|integration|safety|slow|full`` and the release tier (they never call into this module).

Limits: tests that read state outside the repo (a data root, the network, the clock, environment variables) cannot be
validated by file content; such tests must not be cached, which is why only the plain ``fast`` tier is eligible and the
cache is opt-in. Dynamic imports (``importlib``) are not followed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research_speed import importgraph as IG  # noqa: E402
from research_speed.segments import atomic_write_json  # noqa: E402

CACHE_SCHEMA = "test-result-cache-1"


def cache_dir() -> Path:
    env = os.environ.get("TEST_RESULT_CACHE_DIR")
    if env:
        return Path(env)
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
    return (
        base
        / "Temp"
        / "trader_test_result_cache"
        / hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    )


def _conftest_module():
    spec = importlib.util.spec_from_file_location(
        "_tests_conftest_for_cache", ROOT / "tests" / "conftest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def ineligible_reason(rel: str, conf=None) -> str | None:
    """Why a test file may NOT be served from the cache (None = eligible: plain fast tier, no overlay)."""
    c = conf or _conftest_module()
    if rel in c.SLOW_FILES or rel.startswith(c.SLOW_DIRS):
        return "slow tier"
    if rel.startswith(c.INTEGRATION_PREFIXES):
        return "integration tier"
    for name in ("SAFETY_PREFIXES", "CHAOS_PREFIXES", "REPLAY_PREFIXES", "BROKER_PREFIXES"):
        if rel.startswith(getattr(c, name)):
            return name.removesuffix("_PREFIXES").lower() + " overlay"
    if rel in c.SERIAL_FILES:
        return "serial suite"
    return None


def expand_files(targets: list[str]) -> list[str] | None:
    """Explicit test files for a list of file / directory targets; None when a target is not a plain path."""
    out: list[str] = []
    for t in targets:
        if t.startswith("-") or "::" in t or "*" in t:
            return None
        p = ROOT / t
        if p.is_dir():
            out += [f.relative_to(ROOT).as_posix() for f in sorted(p.rglob("test_*.py"))]
        elif p.is_file():
            out.append(t)
        else:
            return None
    return sorted(dict.fromkeys(out))


def _conftests_above(rel: str) -> list[Path]:
    out = []
    d = (ROOT / rel).parent
    while True:
        c = d / "conftest.py"
        if c.is_file():
            out.append(c)
        if d == ROOT:
            break
        d = d.parent
    return out


@dataclass(frozen=True)
class Candidate:
    files: tuple[str, ...]
    fingerprint: str


def fingerprint(files: list[str], extra_args: list[str]) -> str:
    entries = [ROOT / f for f in files]
    for f in files:
        entries += _conftests_above(f)
    roots = [ROOT / "src", ROOT / "scripts", ROOT, *{(ROOT / f).parent for f in files}]
    closure = IG.closure(entries, roots)
    code = IG.hash_files(closure, ROOT)
    h = hashlib.sha256()
    for part in (
        CACHE_SCHEMA,
        sys.version,
        json.dumps(extra_args),
        code,
        IG.content_digest(ROOT / "pyproject.toml"),
        IG.content_digest(ROOT / "uv.lock") if (ROOT / "uv.lock").is_file() else "-",
        _all_files_hash(ROOT / "configs"),
        _all_files_hash(ROOT / "tests" / "fixtures"),
    ):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def _all_files_hash(directory: Path) -> str:
    if not directory.is_dir():
        return "-"
    files = [
        p for p in sorted(directory.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
    ]
    return IG.hash_files(files, ROOT)


def candidate(targets: list[str], extra_args: list[str]) -> tuple[Candidate | None, str]:
    """(candidate, "") when the invocation may use the cache, else (None, reason)."""
    files = expand_files(targets)
    if not files:
        return None, "no explicit test-file list (-m selection / glob / node id)"
    conf = _conftest_module()
    for f in files:
        why = ineligible_reason(f, conf)
        if why:
            return None, f"{f}: {why} (never served from the cache)"
    return Candidate(tuple(files), fingerprint(files, extra_args)), ""


def lookup(c: Candidate, directory: Path | None = None) -> dict | None:
    p = (directory or cache_dir()) / f"{c.fingerprint}.json"
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        rec.get("schema") == CACHE_SCHEMA
        and rec.get("fingerprint") == c.fingerprint
        and rec.get("rc") == 0
        and rec.get("files") == list(c.files)
    ):
        return rec
    return None


def store(c: Candidate, cmd: list[str], wall_s: float, directory: Path | None = None) -> None:
    """Record a GREEN run (call only when pytest exited with 0)."""
    rec = {
        "schema": CACHE_SCHEMA,
        "fingerprint": c.fingerprint,
        "files": list(c.files),
        "rc": 0,
        "cmd": cmd,
        "wall_s": round(wall_s, 1),
        "green_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    atomic_write_json((directory or cache_dir()) / f"{c.fingerprint}.json", rec)
