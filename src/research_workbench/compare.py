# ruff: noqa: E501
"""FIDELITY stage (OFFLINE ONLY): the FAST <-> Nautilus differential as a VERIFIED DAG artifact.

The result of ``research_workbench.differential.run_differential`` is published like every other stage artifact
(atomic payload + sha256 manifest written last) under the FIDELITY key::

    fidelity_key(signals_key, simulation_key, cost, sizing, rules, window, replay_config,
                 nautilus_trader.__version__, closure hash of differential.py + golden.py + replay_backtest.py)

``report`` only accepts an artifact whose key equals the key computed from the CURRENT run record, so a changed signal,
simulation, cost, replay config, nautilus version or differential code can never show a stale PASS; a market that is
now ``REJECT_FAST`` shows no fidelity status at all.

Mapping of the differential status to the promotion status:
PASS -> READY_FOR_ROBUSTNESS | FAIL -> FIDELITY_MISMATCH | BLOCKED / ERROR -> stays PROMOTE_TO_FIDELITY (with the reason).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from research_speed.artifact import config_hash

from . import dag
from .dag import ArtifactStore
from .experiment import ExperimentSpec
from .stage_adapters import candidates_from, json_bytes, load_npz, market_arrays
from .status import PromotionStatus

FIDELITY_FILE = "differential.json"
MAX_FIELD_DIFFS_STORED = 200
_FIDELITY_STATUS = {
    "PASS": "FIDELITY_PASS",
    "FAIL": "FIDELITY_MISMATCH",
    "BLOCKED": "BLOCKED",
    "ERROR": "ERROR",
}
_PROMOTION = {
    "PASS": PromotionStatus.READY_FOR_ROBUSTNESS,
    "FAIL": PromotionStatus.FIDELITY_MISMATCH,
}


def nautilus_version() -> str | None:
    try:
        import nautilus_trader

        return str(nautilus_trader.__version__)
    except Exception:
        return None


def replay_config(experiment: ExperimentSpec) -> dict[str, Any]:
    """The replay configuration ``run_differential`` actually uses for this experiment."""
    from . import differential

    return {
        "timeframe": differential.TIMEFRAME,
        "slippage_one_tick": experiment.cost_model.slippage_pts > 0,
        "instrument": "default",
        "catalog": "synthetic catalog built from the market arrays",
        "tolerances": config_hash(
            {k: asdict(v) for k, v in differential.DEFAULT_TOLERANCES.items()}
        ),
    }


def fidelity_identity(experiment: ExperimentSpec, keys: dict[str, str]) -> dict[str, Any]:
    """``{"key": str | None, "cacheable": bool, "reason": str | None, ...}``; key None => cannot be identified (BLOCKED)."""
    version = nautilus_version()
    if version is None:
        return {"key": None, "cacheable": False, "reason": "nautilus_trader unavailable"}
    try:
        config = replay_config(experiment)
    except Exception as exc:
        return {"key": None, "cacheable": False, "reason": f"differential unavailable: {exc!r}"}
    diff_code = dag.code_hash(dag.DIFF_CODE)
    key = dag.fidelity_key(
        keys["SIGNALS"], keys["SIMULATION"], experiment.cost_model, experiment.sizing,
        experiment.rules, experiment.window, config, version, diff_code,
    )  # fmt: skip
    return {
        "key": key,
        "cacheable": not diff_code.startswith(dag.UNCACHEABLE),
        "reason": None,
        "nautilus_version": version,
        "replay_config": config,
        "diff_code_hash": diff_code,
    }


def read_fidelity(store: ArtifactStore, market: str, key: str) -> dict[str, Any] | None:
    """The stored differential result iff its FIDELITY artifact verifies under exactly ``key``."""
    if not store.lookup(market, "FIDELITY", key).hit:
        return None
    try:
        return json.loads(
            (store.stage_dir(market, "FIDELITY", key) / FIDELITY_FILE).read_text("utf-8")
        )
    except Exception:
        return None


def any_fidelity_artifact(store: ArtifactStore, market: str) -> bool:
    directory = store.root / "dag" / market / "FIDELITY"
    return directory.is_dir() and any(directory.iterdir())


def fidelity_view(
    experiment: ExperimentSpec, store: ArtifactStore, market: str, record: dict[str, Any]
) -> dict[str, Any]:
    """What the report may show about fidelity for the CURRENT keys (NOT_RUN / STALE / the verified result)."""
    if record.get("status") != str(PromotionStatus.PROMOTE_TO_FIDELITY):
        return {
            "FIDELITY_STATUS": "NOT_RUN",
            "DIFFERENTIAL_STATUS": "NOT_RUN",
            "note": f"fast status is {record.get('status')}: no fidelity result is shown",
        }
    ident = fidelity_identity(experiment, record["keys"])
    if ident["key"] is None:
        return {
            "FIDELITY_STATUS": "NOT_RUN",
            "DIFFERENTIAL_STATUS": "NOT_RUN",
            "note": ident["reason"],
        }
    result = read_fidelity(store, market, ident["key"])
    if result is None:
        stale = any_fidelity_artifact(store, market)
        return {
            "FIDELITY_STATUS": "STALE" if stale else "NOT_RUN",
            "DIFFERENTIAL_STATUS": "STALE" if stale else "NOT_RUN",
            "note": "a FIDELITY artifact exists but its key does not match the current keys"
            if stale
            else "run `compare`",
        }
    return {
        "FIDELITY_STATUS": result["fidelity_status"],
        "DIFFERENTIAL_STATUS": result["status"],
        "PROMOTION_STATUS": result["promotion_status"],
        "blocked_reason": result.get("blocked_reason"),
        "scope": result["summary"].get("scope"),
        "by_construction_fields": result["summary"].get("by_construction_fields"),
        "mismatch_leg_counts": result["summary"].get("mismatch_leg_counts"),
        "netting": result["summary"].get("netting"),
        "not_applicable": result["summary"].get("not_applicable"),
        "fast_trade_count": result["fast_trade_count"],
        "fidelity_trade_count": result["fidelity_trade_count"],
        "n_field_diffs": result["n_field_diffs"],
        "nautilus_version": result["nautilus_version"],
        "key": ident["key"],
    }


def run_compare_market(
    experiment: ExperimentSpec, market: str, record: dict[str, Any], store: ArtifactStore
) -> dict[str, Any]:
    """Run (or reuse the verified) differential for a PROMOTE_TO_FIDELITY market and publish the FIDELITY artifact."""
    from . import differential

    ident = fidelity_identity(experiment, record["keys"])
    if ident["key"] is None:
        return {
            "status": "BLOCKED", "fidelity_status": "BLOCKED", "blocked_reason": ident["reason"],
            "promotion_status": str(PromotionStatus.PROMOTE_TO_FIDELITY), "cached": False,
        }  # fmt: skip
    key = ident["key"]
    cached = read_fidelity(store, market, key)
    if cached is not None and cached["status"] != "ERROR":
        return {**cached, "cached": True}
    directory = store.stage_dir(market, "SIGNALS", record["keys"]["SIGNALS"])
    candidates = candidates_from(load_npz(directory / "candidates.npz"))
    market_np = market_arrays(load_npz(directory / "market.npz"))
    scenario = f"{experiment.experiment_id}:{market}"

    def compute():
        result = differential.run_differential(
            market_np, candidates, experiment.cost_model, experiment.sizing, experiment.rules,
            experiment.window, scenario_id=scenario,
        )  # fmt: skip
        promotion = _PROMOTION.get(result.status, PromotionStatus.PROMOTE_TO_FIDELITY)
        payload = {
            "status": result.status,
            "fidelity_status": _FIDELITY_STATUS.get(result.status, "ERROR"),
            "promotion_status": str(promotion),
            "blocked_reason": result.blocked_reason
            or (result.summary.get("error_message") if result.status == "ERROR" else None),
            "scenario_id": scenario,
            "fast_trade_count": result.fast_trade_count,
            "fidelity_trade_count": result.fidelity_trade_count,
            "n_field_diffs": len(result.field_diffs),
            "field_diffs": [asdict(d) for d in result.field_diffs[:MAX_FIELD_DIFFS_STORED]],
            "summary": result.summary,
            "nautilus_version": ident["nautilus_version"],
            "replay_config": ident["replay_config"],
            "signals_key": record["keys"]["SIGNALS"],
            "simulation_key": record["keys"]["SIMULATION"],
        }
        return {FIDELITY_FILE: json_bytes(payload)}, {"status": result.status}, payload

    payload, runtime, _ = store.compute_and_publish(
        experiment.experiment_id, market, "FIDELITY", key, compute,
        code=ident["diff_code_hash"], cacheable=ident["cacheable"],
    )  # fmt: skip
    return {**json.loads(json_bytes(payload)), "cached": False, "runtime_s": runtime}


__all__ = (
    "fidelity_identity",
    "fidelity_view",
    "nautilus_version",
    "read_fidelity",
    "replay_config",
    "run_compare_market",
)
