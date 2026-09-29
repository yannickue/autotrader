"""Run provenance guards shared by the AD1 runners.  Research only.

* ``assert_oos_untouched``: no OOS bar (or any bar beyond Validation) may be in a frame the
  evaluator sees; used by the discovery, survivors and null runners (also for injected
  ``dev_override`` frames).
* ``expected_provenance`` / ``check_pool_provenance``: the survivors runner recomputes the
  identity of the run (config, splits+embargo, dataset, feature cache key, evaluator
  version/fingerprint, Train minimum) and refuses a pool whose meta disagrees.
* ``oos_status``: text banner about the OOS period's AR1 history (NOT a clean holdout).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from alpha.common.protocol import stable_hash
from alpha.discovery.evaluate import EVALUATOR_VERSION, MIN_TRAIN_TRADES, _sha_arrays

REPO_ROOT = Path(__file__).resolve().parents[3]
AR1_OOS_LOG = REPO_ROOT / "research/reports/ar1_phase1/oos_access_log.json"
# toolchain fields (evaluator code + feature build key): may be downgraded to warnings
EVALUATOR_FIELDS = ("evaluator_version", "evaluator_fingerprint", "feature_key")


def assert_oos_untouched(plan: Any, dates: np.ndarray) -> None:
    dates = np.asarray(dates).astype("datetime64[D]")
    n_oos = int(plan.mask(dates, plan.oos).sum())
    assert n_oos == 0, f"{n_oos} OOS bars present in the evaluator frame"
    assert dates.max() <= np.datetime64(plan.validation.end), "bars beyond validation end"


def _norm(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=list)


def expected_provenance(features: Any, evaluator: Any, cfg: dict, plan: Any) -> dict[str, Any]:
    """Identity fields recomputed from the current config / cache / evaluator."""
    return {
        "config_hash": stable_hash(cfg),
        "splits": plan.to_dict(),
        "embargo_days": plan.embargo_days,
        "dataset_hash": _sha_arrays(features, ("ts_ns", "o", "h", "l", "c", "spread")),
        "feature_key": getattr(features, "metadata", {}).get("cache_key"),
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_fingerprint": evaluator._fp_static,
        "min_train_trades": evaluator.min_trades,
    }


def check_pool_provenance(meta: dict[str, Any], expected: dict[str, Any], cfg: dict,
                          allow_evaluator_mismatch: bool = False) -> tuple[list[str], list[str]]:
    """(errors, warnings).  Evaluator version/fingerprint mismatches become warnings only when
    ``allow_evaluator_mismatch`` (the pool is then a mere candidate list, every stage re-runs
    under the CURRENT evaluator)."""
    errors: list[str] = []
    warnings: list[str] = []
    for key, want in expected.items():
        have = meta.get(key)
        if key == "min_train_trades" and have is None:  # pools before the parameter existed
            have = cfg.get("sample_rules", {}).get("min_trades_flag", MIN_TRAIN_TRADES)
        if (key in meta or key == "min_train_trades") and _norm(have) == _norm(want):
            continue
        msg = f"pool meta {key} = {have!r} but recomputed {want!r}" if len(_norm(want)) < 90 \
            else f"pool meta {key} differs from the recomputed value"
        if key not in meta and key != "min_train_trades":
            msg = f"pool meta lacks {key}"
        (warnings if allow_evaluator_mismatch and key in EVALUATOR_FIELDS else errors).append(msg)
    return errors, warnings


def oos_status(log_path: Path | str | None = None) -> str:
    path = Path(log_path) if log_path else AR1_OOS_LOG
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        n = sum(1 for e in entries if e.get("kind") == "evaluation")
        count = f"{n} evaluations"
    except (OSError, ValueError, AttributeError):
        count = "an unknown number of evaluations (access log unreadable)"
    return (
        "AD1 code never reads OOS; the OOS period 2026-05-01..2026-08-31 was previously "
        f"evaluated in AR1 (research/reports/ar1_phase1/oos_access_log.json, {count}) so it is "
        "NOT a clean holdout; use a new forward period for any final test"
    )


__all__ = ("AR1_OOS_LOG", "assert_oos_untouched", "check_pool_provenance",
           "expected_provenance", "oos_status")
