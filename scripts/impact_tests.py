# ruff: noqa: E501
"""Impact-aware test selection (tooling only; no test is removed or weakened, FULL stays available).

Maps changed paths to pytest targets. The mapping is CONSERVATIVE by construction:

* the pre-existing ``run_tests.MATRIX`` (first matching prefix) is kept as is and is only ever EXTENDED here
  (``EXTRA_RULES`` are added on top, every matching rule counts);
* a path that no rule knows never selects less: ``src/``/``scripts/``/``configs/``/``research/`` paths without a rule
  widen to ``-m "fast or integration or safety"``; test helpers / conftest widen to their directory or, for the
  global ones (``tests/conftest.py``, ``pyproject.toml``, ``uv.lock``), to the segmented FULL suite;
* documentation-only changes (``docs/``, ``reports/``, ``*.md`` ...) select no pytest at all, only lint / doc checks.

Tier names (docs/TEST_GATES.md): T0 lint+compile+touched tests (< 60 s), T1 impact (2-5 min, ``changed``),
T2 integration + safety segments, T3 release (full + safety, 20-45 min).
"""

from __future__ import annotations

from dataclasses import dataclass, field

TIER_DOC = {
    "t0": "lint + compile + the touched test files themselves (< 60 s, local)",
    "t1": "impact selection of the changed paths (`changed`, 2-5 min)",
    "t2": "integration + safety segments (merge gate)",
    "t3": "release: full segmented suite + safety overlay (20-45 min; never from the result cache)",
}

# every matching rule counts (union); targets are paths / globs relative to the repo root, "m:<expr>" = a `-m` expression
OBSERVER_TESTS = [
    "tests/unit/market_observer",
    "tests/unit/observer_lab",
    "tests/unit/research_speed",
    "tests/unit/demo/test_observer_parity.py",  # live-vs-batch parity
    "tests/unit/demo/test_observer_store.py",  # observer store
    "tests/unit/demo/opportunity/test_observer_hook.py",
    "tests/unit/demo/runner/test_runner_observer.py",
    "tests/unit/research",
]
EXTRA_RULES: list[tuple[str, list[str], str]] = [
    ("src/market_observer/", OBSERVER_TESTS, ""),
    ("src/coverage_analysis/", OBSERVER_TESTS, ""),
    ("src/research_speed/", OBSERVER_TESTS, ""),
    ("src/demo/observer_store.py", OBSERVER_TESTS, ""),
    ("scripts/observer_", OBSERVER_TESTS, ""),
    ("scripts/run_tests.py", ["tests/unit/scripts/test_run_tests_tiers.py"], ""),
    ("scripts/impact_tests.py", ["tests/unit/scripts/test_run_tests_tiers.py"], ""),
    ("scripts/test_result_cache.py", ["tests/unit/scripts/test_run_tests_tiers.py"], ""),
    (
        "src/execution/",
        ["tests/unit/persistence", "tests/unit/demo/execution", "m:safety"],
        "execution changed: run the broker canary manually before any release "
        "(scripts/e2_broker_canary.py; never started from the test runner)",
    ),
    ("src/risk/", ["tests/unit/portfolio", "m:safety"], ""),
    (
        "src/exits/",
        ["tests/unit/exits", "tests/unit/execution", "tests/unit/demo/execution", "m:safety"],
        "exits changed: exit engine + protection (stops/targets) + execution integration; "
        "canary manually before release",
    ),
]

DOC_PREFIXES = ("docs/", "reports/")
DOC_SUFFIXES = (".md", ".txt", ".rst")
GLOBAL_PATHS = frozenset({"pyproject.toml", "uv.lock", "tests/conftest.py"})
WIDE_MARK = "fast or integration or safety"


@dataclass
class Plan:
    targets: list[str] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    widened: list[str] = field(default_factory=list)  # paths that fell back to a wider selection
    docs_only: bool = False
    full: bool = False  # global change: the segmented FULL suite


def is_doc(path: str) -> bool:
    return path.startswith(DOC_PREFIXES) or path.endswith(DOC_SUFFIXES)


def _add(plan: Plan, target: str) -> None:
    if target.startswith("m:"):
        plan.marks.append(target[2:])
    else:
        plan.targets.append(target)


def impact(paths: list[str], matrix: list[tuple[str, list[str]]]) -> Plan:
    """Translate changed paths into a ``Plan`` (see module docstring for the rules)."""
    plan = Plan()
    non_doc = 0
    for raw in paths:
        p = raw.replace("\\", "/")
        if is_doc(p):
            continue
        non_doc += 1
        if p in GLOBAL_PATHS:
            plan.full = True
            plan.notes.append(f"{p}: global tooling/config change -> segmented FULL suite")
            continue
        if p.startswith("tests/"):
            name = p.rsplit("/", 1)[-1]
            if name.startswith("test_") and name.endswith(".py"):
                plan.targets.append(p)
            elif (
                p.count("/") >= 3
            ):  # helper / conftest / fixture: the whole directory below tests/<a>/
                top = "/".join(p.split("/")[:3])
                plan.targets.append(top)
                plan.widened.append(p)
            else:
                plan.full = True
                plan.notes.append(f"{p}: shared test helper -> segmented FULL suite")
            continue
        hit = False
        for prefix, tgt in matrix:  # the pre-existing semantics: first matching prefix
            if prefix != "tests/" and p.startswith(prefix):
                for t in tgt:
                    _add(plan, t)
                hit = True
                break
        for prefix, tgt, note in EXTRA_RULES:  # additions: every matching rule
            if p.startswith(prefix):
                for t in tgt:
                    _add(plan, t)
                if note and note not in plan.notes:
                    plan.notes.append(note)
                hit = True
        if not hit:
            plan.marks.append(WIDE_MARK)  # unknown path: never select less
            plan.widened.append(p)
    plan.docs_only = bool(paths) and non_doc == 0
    plan.targets = list(dict.fromkeys(plan.targets))
    plan.marks = list(dict.fromkeys(plan.marks))
    return plan
