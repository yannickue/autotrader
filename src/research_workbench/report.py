# ruff: noqa: E501
"""ONE standard research report (dict + markdown), rendered from STORED artifacts only (OFFLINE ONLY).

The report never recomputes anything: it verifies the stage artifacts named in the run record (``ArtifactStore.lookup``)
and reads their payload. A missing/invalid artifact is reported as ``NOT_AVAILABLE`` with the reason.

Sections per market: ENTRY QUALITY, EXIT QUALITY (CAPTURE) and JOINT STRATEGY RESULT are kept separate on purpose:
a negative final trade is not a bad entry and a good entry can be given back by the exit.
"""

from __future__ import annotations

import json
from typing import Any

from alpha.fast.store import FEATURE_SCHEMA_VERSION, FEATURE_SET_VERSION

from .compare import fidelity_view
from .dag import ArtifactStore
from .entry_exit_adapter import CAVEAT
from .experiment import CAUSALITY_STATEMENT, ExperimentSpec

REPORT_VERSION = "wb-report-1"
_ENTRY_EXIT_STAGE = "ENTRY_EXIT"


def _read_artifact(store: ArtifactStore, market: str, stage: str, key: str, file: str) -> Any:
    lk = store.lookup(market, stage, key)
    if not lk.hit:
        return {"status": "NOT_AVAILABLE", "reason": f"{stage} artifact {lk.reason}"}
    try:
        return json.loads((store.stage_dir(market, stage, key) / file).read_text("utf-8"))
    except Exception as exc:
        return {"status": "NOT_AVAILABLE", "reason": f"{stage} unreadable: {type(exc).__name__}"}


def _entry_quality(entry_exit: dict[str, Any] | None) -> dict[str, Any]:
    if not entry_exit or entry_exit.get("status") != "COMPLETE":
        reason = (entry_exit or {}).get("reason") or "run `fast --entry-exit` to produce it"
        return {"status": "NOT_RUN", "reason": reason}
    eq = entry_exit["entry_quality"]
    s = eq["summary"]
    keys = (
        "n", "n_event_clusters", "flag", "mfe_mean", "mfe_median", "mae_mean", "mae_median", "p_mfe_ge",
        "median_time_to_mfe_s", "median_time_to_mae_s", "share_mfe_first", "share_mae_first",
        "useful_entry_share", "entry_failure_share", "label_shares", "verdict",
    )  # fmt: skip
    return {
        "status": "COMPLETE",
        "independent_of_real_exit": True,
        **{k: s.get(k) for k in keys},
        "n_mapped": eq["n_mapped"],
        "n_excluded": eq["n_excluded"],
        "n_sampled": eq["n_sampled"],
        "path_definition": eq["path_definition"],
        "caveat": entry_exit["caveat"],
    }


def _exit_quality(contract: dict[str, Any], entry_exit: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "strategy_exit": {
            "capture": contract.get("capture"),
            "giveback": contract.get("giveback"),
            "holding_time_min_mean": contract.get("holding_time_min_mean"),
            "right_tail_realized_mfe": contract.get("right_tail"),
        },
        "same_entry_comparison": {"status": "NOT_RUN", "reason": "run `fast --entry-exit`"},
        "random_entry_control": {
            "status": "NOT_RUN",
            "reason": "requires the entry/exit diagnostic",
        },
        "caveat": CAVEAT,
    }
    if entry_exit and entry_exit.get("status") == "COMPLETE":
        eq = entry_exit["exit_quality"]
        out["strategy_exit"]["right_tail_potential_mfe"] = eq["strategy_exit_right_tail"]
        out["same_entry_comparison"] = eq["same_entry_comparison"]
        out["random_entry_control"] = entry_exit["random_entry_control"]
        out["not_available"] = entry_exit.get("not_available", [])
        sec = eq["same_entry_comparison"]
        flagged = [
            name
            for name, v in (sec.get("right_tail_by_policy") or {}).items()
            if (v.get("paired_vs_baseline") or {}).get("FLAG_higher_capture_lower_right_tail")
        ]
        out["flags_higher_capture_lower_right_tail"] = flagged
    return out


def _joint(metrics: dict[str, Any]) -> dict[str, Any]:
    c = metrics["contract"]
    keys = (
        "trade_count", "days", "trades_per_day", "expectancy_R", "median_R", "win_rate", "gross_PnL",
        "net_PnL", "cost_burden", "max_drawdown_R", "MFE", "MAE", "capture", "giveback",
        "favorable_before_adverse", "holding_time_min_mean", "holding_time_min_median",
    )  # fmt: skip
    return {
        **{k: c.get(k) for k in keys},
        "train": metrics["train"],
        "validation": metrics["validation"],
        "oos": metrics["oos"] if metrics["partitions_read"].get("OOS") else "NOT_READ",
        "fast_trades": metrics.get("fast_trades"),
        "candidates_blocked_while_busy": metrics.get(
            "candidates_blocked_while_busy", "not computed"
        ),
    }


def build_report(
    experiment: ExperimentSpec,
    store: ArtifactStore,
    records: dict[str, dict[str, Any]],
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exp_id = experiment.experiment_id
    markets: dict[str, Any] = {}
    for market in experiment.markets:
        rec = records.get(market)
        if rec is None or rec.get("status") == "FAILED":
            markets[market] = {
                "FAST_STATUS": "NOT_RUN",
                "reason": (rec or {}).get("error", "no run record"),
            }
            continue
        metrics = _read_artifact(store, market, "METRICS", rec["keys"]["METRICS"], "metrics.json")
        if metrics.get("status") == "NOT_AVAILABLE" and rec.get("metrics_inline"):
            metrics = rec[
                "metrics_inline"
            ]  # METRICS cache write was skipped: the run record carries the result
        if metrics.get("status") == "NOT_AVAILABLE":
            markets[market] = {"FAST_STATUS": "NOT_AVAILABLE", "reason": metrics["reason"]}
            continue
        entry_exit = None
        ee = rec.get("entry_exit")
        if ee:
            entry_exit = _read_artifact(
                store, market, _ENTRY_EXIT_STAGE, ee["key"], "entry_exit.json"
            )
        fid = fidelity_view(experiment, store, market, rec)
        markets[market] = {
            "DATASET_HASH": rec["dataset_hash"],
            "N_BARS": rec["n_bars"],
            "FAST_STATUS": metrics["status"],
            "FAST_REJECT_REASONS": metrics["reasons"],
            "PROMOTION_STATUS": fid.get("PROMOTION_STATUS", metrics["status"]),
            "FIDELITY_STATUS": fid["FIDELITY_STATUS"],
            "DIFFERENTIAL_STATUS": fid["DIFFERENTIAL_STATUS"],
            "FIDELITY": fid,
            "DATA_SCOPE": rec.get("data_scope"),
            "ROBUSTNESS_STATUS": "NOT_RUN",
            "OOS_STATUS": "READ" if metrics["partitions_read"].get("OOS") else "NOT_READ",
            "ARTIFACTS": {
                s: {
                    "key": v["key"],
                    "cache": v["cache"],
                    "cache_write_skipped": v.get("cache_write_skipped"),
                    "state": (
                        f"COMPLETE (cache write skipped: {v['cache_write_skipped']})"
                        if v.get("cache_write_skipped")
                        else v["cache"]
                    ),
                    "computed": v["computed"],
                    "runtime_s": v["runtime_s"],
                    "dir": str(store.stage_dir(market, s, v["key"])),
                }
                for s, v in rec["stages"].items()
            },
            "ENTRY_QUALITY": _entry_quality(entry_exit),
            "EXIT_QUALITY_CAPTURE": _exit_quality(metrics["contract"], entry_exit),
            "JOINT_STRATEGY_RESULT": _joint(metrics),
            "SKIPS": metrics["skips"],
            "N_CANDIDATES": metrics["n_candidates"],
        }
    report = {
        "REPORT_VERSION": REPORT_VERSION,
        "EXPERIMENT_ID": exp_id,
        "EXPERIMENT_HASH": experiment.experiment_hash(),
        "STRATEGY_ID": experiment.strategy_spec.strategy_id,
        "SPEC_HASH": experiment.strategy_spec.spec_hash(),
        "MARKETS": markets,
        "FEATURE_VERSION": {"feature_set_version": FEATURE_SET_VERSION, "schema_version": FEATURE_SCHEMA_VERSION},
        "COST_MODEL": experiment.cost_model.__dict__,
        "SIZING": experiment.sizing.__dict__,
        "SPLIT": experiment.split.to_dict(),
        "PARTITIONS_READ": experiment.partitions_actually_read(),
        "ENTRY_EXIT_DIAGNOSTIC_CAVEAT": CAVEAT,
        "CAUSALITY_STATEMENT": CAUSALITY_STATEMENT,
    }  # fmt: skip
    if (
        coverage is not None
    ):  # read-only coverage audit of a demo DB copy (research_workbench.coverage)
        report["COVERAGE"] = coverage
    return report


def _f(value: Any, nd: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{nd}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Research report {report['EXPERIMENT_ID']}",
        "",
        f"- EXPERIMENT_ID: `{report['EXPERIMENT_ID']}`",
        f"- STRATEGY_ID: `{report['STRATEGY_ID']}`  SPEC_HASH: `{report['SPEC_HASH']}`",
        f"- FEATURE_VERSION: {report['FEATURE_VERSION']}",
        f"- COST_MODEL: {report['COST_MODEL']}",
        f"- SIZING: {report['SIZING']}",
        f"- SPLIT: {report['SPLIT']}",
        "- PARTITIONS READ: "
        + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in report["PARTITIONS_READ"].items()),
        "",
    ]
    for market, m in report["MARKETS"].items():
        lines += [f"## Market {market}", ""]
        if "DATASET_HASH" not in m:
            lines += [f"FAST STATUS: {m['FAST_STATUS']} ({m.get('reason')})", ""]
            continue
        lines += [
            f"- DATASET_HASH: `{m['DATASET_HASH']}` ({m['N_BARS']} bars)",
            f"- FAST STATUS: **{m['FAST_STATUS']}** reasons={m['FAST_REJECT_REASONS']}",
            f"- PROMOTION STATUS: {m['PROMOTION_STATUS']}",
            f"- FIDELITY STATUS: {m['FIDELITY_STATUS']}  DIFFERENTIAL STATUS: {m['DIFFERENTIAL_STATUS']}",
            f"- ROBUSTNESS STATUS: {m['ROBUSTNESS_STATUS']}  OOS STATUS: {m['OOS_STATUS']}",
            "- ARTIFACTS: " + "; ".join(f"{s}:{a['state']}" for s, a in m["ARTIFACTS"].items()),
            *_fidelity_lines(m["FIDELITY"]),
            *_scope_lines(m.get("DATA_SCOPE")),
            "",
            "### ENTRY QUALITY",
            "",
        ]
        eq = m["ENTRY_QUALITY"]
        if eq["status"] != "COMPLETE":
            lines += [f"{eq['status']}: {eq.get('reason')}", ""]
        else:
            lines += [
                f"N={eq['n']} (event clusters {eq['n_event_clusters']}, flag: {eq['flag']}); path: {eq['path_definition']}",
                f"MFE_R mean/median {_f(eq['mfe_mean'])}/{_f(eq['mfe_median'])}; MAE_R mean/median {_f(eq['mae_mean'])}/{_f(eq['mae_median'])}",
                f"time-to-MFE/MAE median s: {_f(eq['median_time_to_mfe_s'], 0)}/{_f(eq['median_time_to_mae_s'], 0)}; "
                f"MFE-first {_f(eq['share_mfe_first'])} MAE-first {_f(eq['share_mae_first'])}",
                "P(MFE>=x): "
                + ", ".join(f"{k}R={_f(v)}" for k, v in (eq["p_mfe_ge"] or {}).items()),
                f"useful {_f(eq['useful_entry_share'])} failure {_f(eq['entry_failure_share'])}; verdict {eq['verdict']}",
                f"_{eq['caveat']}_",
                "",
            ]
        lines += ["### EXIT QUALITY (CAPTURE)", ""]
        xq = m["EXIT_QUALITY_CAPTURE"]
        se = xq["strategy_exit"]
        lines.append(
            f"Strategy exit: capture {_f(se['capture'])}, giveback {_f(se['giveback'])} R, holding {_f(se['holding_time_min_mean'], 1)} min"
        )
        lines += _right_tail_lines(
            "Right tail (realized vs trade MFE)", se["right_tail_realized_mfe"]
        )
        if "right_tail_potential_mfe" in se:
            lines += _right_tail_lines(
                "Right tail (realized vs potential MFE)", se["right_tail_potential_mfe"]
            )
        sc = xq["same_entry_comparison"]
        if sc["status"] == "COMPLETE":
            lines += [
                f"Same-entry exit-policy lab on N={sc['n_entries']} sampled entries (basis: {sc['basis']}). Per-policy rows use each policy's OWN applicable entries; every policy-vs-fixed_1_5r comparison and the capture/right-tail FLAG use PAIRED entries only (both policies applicable and capture defined):",
                "",
            ]
            lines += [
                "| policy | own n | mean R | capture | right tail P(R>=2/3/5) | paired entries vs fixed_1_5r (excluded) | FLAG |",
                "|---|---|---|---|---|---|---|",
            ]
            for name, p in sc["summary"]["policies"].items():
                tail = (sc["right_tail_by_policy"].get(name) or {}).get("right_tail") or {}
                ps = (
                    "/".join(_f(v["p_realized_ge"], 2) for v in (tail.get("levels") or {}).values())
                    or "-"
                )
                pv = (sc["right_tail_by_policy"].get(name) or {}).get("paired_vs_baseline") or {}
                paired = (
                    f"{pv['n_pairs']} ({pv['n_excluded_not_both_applicable'] + pv['n_excluded_capture_undefined']})"
                    if pv
                    else "-"
                )
                flag = "YES" if pv.get("FLAG_higher_capture_lower_right_tail") else "-"
                lines.append(
                    f"| {name} | {p['n_applicable']} | {_f(p['mean_r'])} | {_f(p['mean_capture_ratio'])} | {ps} | {paired} | {flag} |"
                )
            flagged = xq.get("flags_higher_capture_lower_right_tail") or []
            lines += [
                "",
                "FLAG higher capture but lower right tail vs fixed_1_5r (paired entries): "
                + (", ".join(flagged) or "none"),
                "",
            ]
        else:
            lines += [f"Same-entry comparison: {sc['status']} {sc.get('reason', '')}", ""]
        rc = xq["random_entry_control"]
        lines += [
            f"Random-entry control: {rc['status']} ({rc.get('reason', '')})",
            f"_{xq['caveat']}_",
            "",
        ]
        lines += ["### JOINT STRATEGY RESULT", ""]
        j = m["JOINT_STRATEGY_RESULT"]
        lines += [
            f"trades {j['trade_count']} over {j['days']} days ({_f(j['trades_per_day'])}/day); expectancy {_f(j['expectancy_R'])} R, median {_f(j['median_R'])} R, win rate {_f(j['win_rate'])}",
            f"FAST trades {j['fast_trades']}; candidates blocked while busy {j['candidates_blocked_while_busy']} (FAST enforces one position at a time via next_free; disclosure only, no netting-adjusted count)",
            f"gross PnL {_f(j['gross_PnL'], 1)} EUR, net PnL {_f(j['net_PnL'], 1)} EUR, cost burden {_f(j['cost_burden'])} R, max drawdown {_f(j['max_drawdown_R'])} R",
            f"MFE {_f(j['MFE'])} MAE {_f(j['MAE'])} capture {_f(j['capture'])} giveback {_f(j['giveback'])}; favorable-before-adverse: {j['favorable_before_adverse']}",
            f"TRAIN n={j['train']['n_trades']} exp={_f(j['train']['expectancy_r'])}; VALIDATION n={j['validation']['n_trades']} exp={_f(j['validation']['expectancy_r'])}; OOS: "
            + (_f(j["oos"]["expectancy_r"]) if isinstance(j["oos"], dict) else str(j["oos"])),
            "",
        ]
    lines += [f"> {report['ENTRY_EXIT_DIAGNOSTIC_CAVEAT']}", ""]
    if report.get("COVERAGE") is not None:  # read-only coverage audit (research_workbench.coverage)
        from .coverage import render_markdown as render_coverage

        lines += ["---", "", render_coverage(report["COVERAGE"])]
    return "\n".join(lines)


def _fidelity_lines(fid: dict[str, Any]) -> list[str]:
    out = []
    if fid.get("note"):
        out.append(f"- FIDELITY note: {fid['note']}")
    if fid.get("scope"):
        out += [
            f"- DIFFERENTIAL SCOPE: {fid['scope']}",
            f"- BY_CONSTRUCTION fields (copied from FAST, not independent): {fid.get('by_construction_fields')}",
            f"- mismatch legs: {fid.get('mismatch_leg_counts')}; blocked/error reason: {fid.get('blocked_reason')}",
            f"- trades fast/fidelity: {fid.get('fast_trade_count')}/{fid.get('fidelity_trade_count')}; nautilus {fid.get('nautilus_version')}",
            f"- netting: candidates blocked by open position {(fid.get('netting') or {}).get('candidates_blocked_by_open_position')}, overlap violations {(fid.get('netting') or {}).get('overlap_violations')}, same-bar re-entries {(fid.get('netting') or {}).get('same_bar_reentries')}",
        ]
    return out


def _scope_lines(scope: dict[str, Any] | None) -> list[str]:
    if not scope:
        return []
    bl = scope["bars_loaded"]
    return [
        f"- BARS LOADED: {bl['n_bars']} ({bl['first_date']}..{bl['last_date']}); warm-up before TRAIN {scope['warmup_before_train_bars']}, "
        f"gap/embargo {scope['gap_or_embargo_bars']}; bars in partitions {scope['bars_in_partitions']}",
        f"- PARTITIONS FEEDING METRICS: {scope['partitions_feeding_metrics']} ({scope['bars_feeding_metrics']} bars; "
        f"{scope['bars_loaded_but_not_in_metrics']} loaded bars are NOT used by any metric)",
        f"- CAUSALITY / LIMITATION: {scope['causality']}",
    ]


def _right_tail_lines(title: str, tail: dict[str, Any] | None) -> list[str]:
    if not tail:
        return [f"{title}: -"]
    parts = []
    for level, v in tail["levels"].items():
        parts.append(
            f"{level}: P(R>=L)={_f(v['p_realized_ge'])}, N(MFE>=L)={v['n_mfe_ge']}, mean/median R|MFE>=L={_f(v['mean_realized_given_mfe_ge'])}/{_f(v['median_realized_given_mfe_ge'])}, excluded non-finite in bucket={v.get('n_bucket_excluded_nonfinite', 0)}"
        )
    head = (
        f"{title} (N total={tail.get('n_total', tail['n'])}, finite={tail.get('n_finite', tail['n'])}, "
        f"excluded non-finite={tail.get('n_excluded_nonfinite', 0)})"
    )
    return [
        f"{head}, excluded non-finite MFE={tail.get('n_excluded_nonfinite_mfe', 0)}: "
        + " | ".join(parts)
    ]


__all__ = ("REPORT_VERSION", "build_report", "render_markdown")
