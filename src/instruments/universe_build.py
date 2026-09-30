# ruff: noqa: E501
"""OFFLINE builder (Lane U): universe list + probe raw -> inventory JSON/MD + shadow-only market TOMLs.

Deterministic: the output depends only on the two input documents (no wall clock, stable ordering).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from instruments.universe import (
    SELECTION_RULE_ID,
    assess,
    canonical_for,
    classify_cluster,
    estimate_server_offset_s,
    unprobed_reason,
)

SCHEMA_VERSION = 1
ACTIVE_DISCOVERY_UNIVERSE: dict[str, str] = {  # canonical -> broker symbol
    "GER40": "Ger40",
    "NAS100": "UsaTec",
    "SPX500": "Usa500",
    "XAUUSD": "GOLD",
    "EURUSD": "EURUSD",
    "BTCUSD": "BTCUSD",
    "BRENT": "Brent",
}
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_INFO_KEYS = (
    "digits", "point", "trade_tick_size", "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss",
    "trade_contract_size", "volume_min", "volume_max", "volume_step", "trade_stops_level", "trade_freeze_level",
    "swap_mode", "swap_long", "swap_short", "currency_base", "currency_profit", "currency_margin", "trade_mode",
    "trade_calc_mode", "spread", "spread_float", "visible",
)  # fmt: skip


def _fx_mids(probes: dict[str, dict]) -> dict[str, float]:
    mids = {}
    for name, p in probes.items():
        t = p.get("tick_now")
        if len(name) == 6 and name.isalpha() and name.isupper() and t and t.get("bid") and t.get("ask"):
            mids[name] = 0.5 * (t["bid"] + t["ask"])
    return mids


def _session_summary(probe: dict, offset_s: int) -> dict[str, Any]:
    """Modal daily window per weekday in server clock and UTC (minutes since midnight)."""
    wd = (probe.get("sessions_m5") or {}).get("weekday_median_first_last_minute_server") or {}
    out: dict[str, Any] = {}
    off_min = offset_s // 60
    for k, (first, last, ndays) in sorted(wd.items(), key=lambda kv: int(kv[0])):
        out[_WEEKDAYS[int(k)]] = {
            "server_first_min": first,
            "server_last_min": last,
            "utc_first_min": (first - off_min) % 1440,
            "utc_last_min": (last - off_min) % 1440,
            "days_observed": ndays,
        }
    return out


def build_inventory(listing_doc: dict, raw_doc: dict) -> dict[str, Any]:
    listing = sorted(listing_doc["symbols"], key=lambda r: r["name"])
    probes: dict[str, dict] = raw_doc.get("probes", {})
    selected = {s["name"] for s in raw_doc.get("selection", [])}
    offset = estimate_server_offset_s(probes)
    mids = _fx_mids(probes)
    active_symbols = set(ACTIVE_DISCOVERY_UNIVERSE.values())
    rows: list[dict[str, Any]] = []
    for r in listing:
        name = r["name"]
        cluster = classify_cluster(r)
        row: dict[str, Any] = {
            "symbol": name,
            "canonical": canonical_for(name),
            "description": r.get("description", ""),
            "path": r.get("path", ""),
            "cluster": cluster,
            "universe": "ACTIVE" if name in active_symbols else "SHADOW",
            "trade_mode": r.get("trade_mode"),
            "probed": name in selected,
        }
        if name in selected and name in probes:
            p = probes[name]
            a = assess(r, p, mids=mids, server_offset_s=offset)
            info = p.get("symbol_info") or {}
            row["contract"] = {k: info.get(k) for k in _INFO_KEYS if k in info}
            row["sessions_server_clock"] = _session_summary(p, offset)
            row["gap_patterns_server_clock"] = (p.get("sessions_m5") or {}).get("gap_patterns_server_clock", {})
            row["history"] = {
                "m5": {k: (p.get("rates_m5") or {}).get(k) for k in ("n", "first_bar_server_epoch", "last_bar_server_epoch")},
                "m1": {k: (p.get("rates_m1") or {}).get(k) for k in ("n", "first_bar_server_epoch", "last_bar_server_epoch")},
            }
            row["facts"] = a["facts"]
            row["verdict"] = a["verdict"]
            row["reasons"] = a["reasons"]
            row["retryable_reasons"] = a["retryable_reasons"]
            row["readiness_pending_quote"] = bool(
                a["verdict"] == "NOT_READY" and a["retryable_reasons"] and len(a["reasons"]) == len(a["retryable_reasons"])
            )
        else:
            row["readiness_pending_quote"] = False
            row["verdict"] = "NOT_READY"
            row["reasons"] = [unprobed_reason(r, selected) or "probe_missing"]
            row["retryable_reasons"] = []
        rows.append(row)
    by_cluster = Counter(r["cluster"] for r in rows)
    ready = [r for r in rows if r["verdict"] == "SHADOW_READY"]
    retry = [r for r in rows if r["readiness_pending_quote"]]
    orig_visible = {r["name"] for r in listing if r.get("visible")}
    watch_selected = sorted(n for n in selected if n not in orig_visible)
    reasons = Counter(x.split("_")[0] + "_" + x.split("_")[1] if x.count("_") >= 1 else x for r in rows if r["verdict"] == "NOT_READY" for x in r["reasons"])
    return {
        "schema_version": SCHEMA_VERSION,
        "selection_rule_id": SELECTION_RULE_ID,
        "sources": {"list_retrieved_at": listing_doc.get("retrieved_at"), "probe_retrieved_at": raw_doc.get("retrieved_at")},
        "account_currency": listing_doc.get("account_currency"),
        "server_utc_offset_s": offset,
        "active_discovery_universe": ACTIVE_DISCOVERY_UNIVERSE,
        "market_watch_selected_by_scan": watch_selected,
        "market_watch_restored_after_scan": True,
        "counts": {
            "listed": len(rows),
            "probed": sum(1 for r in rows if r["probed"]),
            "shadow_ready": len(ready),
            "not_ready": len(rows) - len(ready),
            "pending_quote_recheck_when_open": len(retry),
            "pending_quote_by_cluster": dict(sorted(Counter(r["cluster"] for r in retry).items())),
            "by_cluster": dict(sorted(by_cluster.items())),
            "shadow_ready_by_cluster": dict(sorted(Counter(r["cluster"] for r in ready).items())),
            "not_ready_reason_prefixes": dict(reasons.most_common()),
        },
        "symbols": rows,
    }


# ------------------------------------------------------------------ shadow TOML


def _q(s: Any) -> str:
    return json.dumps(str(s), ensure_ascii=False)


def _num(x: float) -> str:
    return repr(float(x))


def render_shadow_toml(row: dict[str, Any], offset_s: int, snapshot_date: str) -> str:
    pending = bool(row.get("readiness_pending_quote"))
    c, f = row["contract"], row["facts"]
    point = float(c["point"])
    med_price = float(f["spread_median_points"]) * point
    p95_price = float(f["spread_p95_points"]) * point
    med_price = med_price if med_price > 0 else point
    cap = min(max(p95_price, med_price) * 1.25, med_price * 10)
    lines = [
        f"# SHADOW-ONLY market spec for broker symbol {row['symbol']} (Lane U, generated by scripts/universe_inventory.py build).",
        "# mode = \"shadow_only\": observed/replayed only, NEVER tradable (no enablement switch exists; see src/markets/shadow.py).",
        "# Calendar is PROVISIONAL, derived from observed M5 bar timestamps; cost inputs are OBSERVED recorded spreads.",
        "",
        "[market]",
        f"canonical = {_q(row['canonical'])}",
        f"broker_symbol = {_q(row['symbol'])}",
        f"broker_path = {_q(row['path'])}",
        f"cluster = {_q(row['cluster'])}",
        f"description = {_q(row['description'])}",
        'mode = "shadow_only"',
        "",
        "[instrument]",
        f"point_size = {_num(point)}",
        f"digits = {int(c['digits'])}",
        f"contract_size = {_num(c['trade_contract_size'])}",
        f"tick_size = {_num(c['trade_tick_size'])}",
        f"volume_min = {_num(c['volume_min'])}",
        f"volume_step = {_num(c['volume_step'])}",
        f"volume_max = {_num(c['volume_max'])}",
        f"currency_profit = {_q(c['currency_profit'])}",
        f"currency_margin = {_q(c['currency_margin'])}",
        "",
        "[calendar]",
        'status = "provisional"',
        'tz = "UTC"',
        f"server_utc_offset_s = {int(offset_s)}",
        "",
        "[calendar.sessions_server_clock]",
        "# weekday = [first_bar_minute, last_bar_minute, days_observed] (median per day, minutes since server midnight)",
    ]
    for wd, s in row["sessions_server_clock"].items():
        lines.append(f"{wd} = [{s['server_first_min']}, {s['server_last_min']}, {s['days_observed']}]")
    if not row["sessions_server_clock"]:
        lines.append("# no bars observed")
    lines += [
        "",
        "[cost]",
        f"reference_median_spread_price = {_num(med_price)}",
        f"reference_p95_spread_price = {_num(max(p95_price, med_price))}",
        'spread_source = "observed_rates_m5_recorded_spread_universe_scan"',
        f"swap_mode = {int(c.get('swap_mode') or 0)}",
        f"swap_long_points = {_num(c.get('swap_long') or 0.0)}",
        f"swap_short_points = {_num(c.get('swap_short') or 0.0)}",
        "",
        "[risk]",
        f"max_entry_spread_price = {_num(cap)}",
    ]
    if f.get("implied_leverage"):
        lines.append(f"implied_leverage = {_num(f['implied_leverage'])}")
    if f.get("margin_min_lot_eur"):
        lines.append(f"margin_min_lot_eur = {_num(f['margin_min_lot_eur'])}")
    lines += [
        "",
        "[data_quality]",
        'flags = ["shadow_only", "calendar_provisional", "spread_observed_m5", "commission_unverified", "no_fitted_thresholds"'
        + (', "quote_freshness_unverified_market_closed_at_scan"' if pending else "")
        + "]",
        "",
        "[provenance]",
        f"snapshot_date = {_q(snapshot_date)}",
        "",
    ]
    return "\n".join(lines)


def render_markdown(inv: dict[str, Any]) -> str:
    cnt = inv["counts"]
    out = [
        "# Broker universe inventory (Lane U, stage 1)",
        "",
        f"Selection rule `{inv['selection_rule_id']}`; account currency {inv['account_currency']}; server clock = UTC{inv['server_utc_offset_s'] // 3600:+d} (estimated from fresh ticks).",
        f"Sources: list {inv['sources']['list_retrieved_at']}, probe {inv['sources']['probe_retrieved_at']}.",
        "Read-only scan: no orders, no order_check, no login. Symbols added to Market Watch by the scan: "
        + (", ".join(inv["market_watch_selected_by_scan"]) or "none")
        + " (all deselected again afterwards, Market Watch restored to the 10 originally visible symbols).",
        "",
        f"Listed {cnt['listed']}, probed {cnt['probed']}, SHADOW_READY {cnt['shadow_ready']}, NOT_READY {cnt['not_ready']} "
        f"(of which PENDING only because the quote was stale/absent while the market was closed at scan time: {cnt['pending_quote_recheck_when_open']}; re-scan during open hours).",
        "",
        "## Counts per class",
        "",
        "| cluster | listed | SHADOW_READY |",
        "|---|---|---|",
    ]
    for k, v in cnt["by_cluster"].items():
        out.append(f"| {k} | {v} | {cnt['shadow_ready_by_cluster'].get(k, 0)} |")
    out += ["", "## ACTIVE discovery universe (production specs; not shadow-generated)", ""]
    by_sym = {r["symbol"]: r for r in inv["symbols"]}
    for canon, sym in inv["active_discovery_universe"].items():
        r = by_sym.get(sym)
        out.append(f"- {canon} = `{sym}`: {r['verdict'] if r else 'NOT_LISTED'} {', '.join(r['reasons']) if r else ''}".rstrip())
    out += ["", "## SHADOW_READY (shadow universe)", "", "| symbol | cluster | ccy | spread med/p95 (frac of price) | lev | M5 bars | margin min lot EUR |", "|---|---|---|---|---|---|---|"]
    for r in inv["symbols"]:
        if r["verdict"] != "SHADOW_READY":
            continue
        f = r["facts"]
        out.append(
            f"| {r['symbol']} | {r['cluster']} | {f['currency_profit']} | {f.get('spread_median_frac', 0):.5%} / {f.get('spread_p95_frac', 0):.5%} | "
            f"{f.get('implied_leverage', 0):.1f} | {f['m5_bars']} | {f.get('margin_min_lot_eur') or 0:.2f} |"
        )
    pend = [r for r in inv["symbols"] if r.get("readiness_pending_quote")]
    out += ["", f"## PENDING quote re-check ({len(pend)}): all other gates passed; quote stale/absent because the market was closed at scan time", ""]
    out.append(", ".join(f"{r['symbol']}" for r in pend) or "none")
    out += ["", "## NOT_READY (excluded)", "", "Top reason prefixes: " + ", ".join(f"{k} ({v})" for k, v in list(cnt["not_ready_reason_prefixes"].items())[:10]), ""]
    groups: dict[str, list[str]] = {}
    for r in inv["symbols"]:
        if r["verdict"] == "NOT_READY":
            key = "; ".join(r["reasons"])
            groups.setdefault(key, []).append(r["symbol"])
    for reason, syms in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        shown = ", ".join(syms[:25]) + (f" ... (+{len(syms) - 25})" if len(syms) > 25 else "")
        out.append(f"- **{reason}** ({len(syms)}): {shown}")
    out.append("")
    return "\n".join(out)


def build_outputs(raw_path: Path, repo_root: Path) -> int:
    evid = repo_root / "docs" / "evidence"
    listing_doc = json.loads((evid / "universe_symbol_list.json").read_text(encoding="utf-8"))
    raw_doc = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    inv = build_inventory(listing_doc, raw_doc)
    (evid / "universe_inventory.json").write_text(json.dumps(inv, indent=1, default=str, sort_keys=False), encoding="utf-8")
    (evid / "universe_inventory.md").write_text(render_markdown(inv), encoding="utf-8")
    shadow_dir = repo_root / "configs" / "markets_shadow"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    for old in shadow_dir.glob("*.toml"):
        old.unlink()
    snap = str(raw_doc.get("retrieved_at", ""))[:10]
    n = 0
    for r in inv["symbols"]:
        if (r["verdict"] == "SHADOW_READY" or r.get("readiness_pending_quote")) and r["universe"] == "SHADOW":
            (shadow_dir / f"{r['canonical']}.toml").write_text(
                render_shadow_toml(r, inv["server_utc_offset_s"], snap), encoding="utf-8"
            )
            n += 1
    print(json.dumps(inv["counts"], indent=1))
    print(f"shadow TOMLs written: {n}")
    return 0
