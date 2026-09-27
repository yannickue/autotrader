"""Run every Sprint 1 e2e scenario and write a PASS/FAIL evidence report.

This intentionally reuses the exact `scenario_*` functions that
`tests/integration/test_e2e_paper_path.py` exercises under pytest, so the
evidence file can never drift from what the test suite actually verified.

Usage:
    <PY> scripts/sprint1_e2e_evidence.py
Writes: reports/sprint1_e2e_evidence.json
"""

from __future__ import annotations

import json
import sys
import traceback
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from tests.integration import e2e_scenarios  # noqa: E402  (path setup must precede this)


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("evidence must not contain non-finite Decimals")
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def main() -> int:
    report: dict[str, dict[str, object]] = {}
    failures = 0

    for name, scenario_fn in sorted(e2e_scenarios.ALL_SCENARIOS.items()):
        try:
            raw_evidence = scenario_fn()
            report[name] = {
                "result": "PASS",
                "evidence": _jsonable(raw_evidence),
            }
        except Exception as exc:  # report every scenario failure, keep going
            failures += 1
            report[name] = {
                "result": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }

    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / "sprint1_e2e_evidence.json"
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, sort_keys=True, indent=2, allow_nan=False)

    total = len(report)
    passed = total - failures
    print(
        f"sprint1_e2e_evidence: {passed}/{total} scenarios PASS "
        f"({failures} FAIL) -> {output_path}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
