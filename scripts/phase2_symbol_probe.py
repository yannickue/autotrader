# ruff: noqa: E501
"""Phase-2 symbol probe (Lane M): READ-ONLY, attach-only MT5 DEMO probe for new markets (BRENT, BTCUSD).

Calls ONLY: account_info, terminal_info, symbols_get, symbol_info, symbol_info_tick, copy_rates_from_pos,
order_calc_margin, order_calc_profit. (The last two are pure CALCULATIONS, not orders.) NEVER order_send /
order_check / symbol_select / login. Runs inside the project's hard-timeout bounded child process; if the
global MT5 lock is held (e.g. by the live runner) the parent retries a few times with backoff and then reports
BLOCKED instead of forcing anything.

Usage:
    <PY> scripts/phase2_symbol_probe.py discover [OUT_JSON]              # pattern scan over ALL symbols
    <PY> scripts/phase2_symbol_probe.py probe SYM1,SYM2 [OUT_JSON] [--tick-seconds N]   # deep probe of exact symbols
    <PY> scripts/phase2_symbol_probe.py verdict [PROBE_JSON] [OUT_JSON]   # OFFLINE: preflight GREEN/RED per market
    <PY> scripts/phase2_symbol_probe.py from-snapshot SYM1,SYM2 [OUT_JSON]  # OFFLINE: evidence from the stored snapshot
Nothing is guessed: `probe` takes exact broker symbol names that a human/agent chose from the `discover` output.
"""

from __future__ import annotations

import itertools
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v2_common import REPO_ROOT, load_config

from adapters.activtrades_mt5.bounded import run_worker_bounded
from adapters.activtrades_mt5.connection import MT5Connection
from adapters.activtrades_mt5.real_client import get_real_client
from adapters.config import MT5ConfigError

DEFAULT_OUT = REPO_ROOT / "docs" / "evidence" / "phase2_symbol_probe.json"
DISCOVER_OUT = REPO_ROOT / "docs" / "evidence" / "phase2_symbol_discovery.json"

OIL_RE = re.compile(r"brent|oil|crude|ukoil|\bbrn|xbr|\buko|\bwti|xti|\bcl[a-z0-9]?$", re.IGNORECASE)
CRYPTO_RE = re.compile(r"btc|bitcoin|crypto", re.IGNORECASE)

TF_M1, TF_M5, TF_H1 = 1, 5, 16385
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1


def _plain(value):
    return value if isinstance(value, (int, float, str, bool)) or value is None else str(value)


def _info_full(info) -> dict:
    if info is None:
        return {}
    if hasattr(info, "_asdict"):
        return {k: _plain(v) for k, v in info._asdict().items()}
    return {f: _plain(getattr(info, f)) for f in dir(info) if not f.startswith("_") and not callable(getattr(info, f))}


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, round(p / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def _dist(vals: list[float]) -> dict:
    s = sorted(vals)
    return {
        "n": len(s),
        "min": s[0] if s else None,
        "p25": _pct(s, 25),
        "median": _pct(s, 50),
        "p75": _pct(s, 75),
        "p95": _pct(s, 95),
        "p99": _pct(s, 99),
        "max": s[-1] if s else None,
    }


def _rates_block(client, symbol: str, tf: int, n: int) -> tuple[list, dict]:
    rates = client.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        return [], {"available": False}
    times = [int(r["time"]) for r in rates]
    spreads = [float(r["spread"]) for r in rates]
    return list(rates), {
        "available": True,
        "n": len(times),
        "first_bar_server_epoch": times[0],
        "last_bar_server_epoch": times[-1],
        "first_bar_server_clock": datetime.fromtimestamp(times[0], UTC).isoformat(),
        "last_bar_server_clock": datetime.fromtimestamp(times[-1], UTC).isoformat(),
        "spread_points": _dist(spreads),
    }


def _session_analysis(rates: list, step_s: int) -> dict:
    """Observed trading-hours structure from bar timestamps (server wall clock encoded as epoch)."""
    times = [int(r["time"]) for r in rates]
    gaps = []
    for a, b in itertools.pairwise(times):
        if b - a > 15 * 60:
            gaps.append((a, b))
    by_wd_open: dict[int, list[int]] = {}
    by_wd_minutes: dict[int, set[int]] = {}
    for t in times:
        dt = datetime.fromtimestamp(t, UTC)
        by_wd_minutes.setdefault(dt.weekday(), set()).add(dt.hour * 60 + dt.minute)
    weekday_hours = {}
    for wd, mins in sorted(by_wd_minutes.items()):
        hours = sorted({m // 60 for m in mins})
        weekday_hours[str(wd)] = {"hours_with_bars": hours, "first_minute": min(mins), "last_minute": max(mins)}
    # gap histogram by (start weekday, start hh:mm, duration minutes)
    gap_rows: dict[str, int] = {}
    for a, b in gaps:
        da = datetime.fromtimestamp(a, UTC)
        db = datetime.fromtimestamp(b, UTC)
        key = f"wd{da.weekday()} {da.strftime('%H:%M')} -> wd{db.weekday()} {db.strftime('%H:%M')} ({(b - a) // 60}min)"
        gap_rows[key] = gap_rows.get(key, 0) + 1
    by_wd_open.clear()
    return {
        "bars": len(times),
        "gaps_gt_15min": len(gaps),
        "gap_patterns_server_clock": dict(sorted(gap_rows.items(), key=lambda kv: -kv[1])[:40]),
        "weekday_hours_server_clock": weekday_hours,
        "weekdays_with_bars": sorted({datetime.fromtimestamp(t, UTC).weekday() for t in times}),
        "note": "server clock encoded as UTC epoch; weekday 0=Mon..6=Sun",
    }


def _probe_symbol(client, symbol: str, tick_seconds: float, now_utc: datetime) -> dict:
    out: dict = {"symbol": symbol}
    info = client.symbol_info(symbol)
    out["symbol_info"] = _info_full(info)
    if info is None:
        out["error"] = "symbol_info returned None"
        return out
    tick = client.symbol_info_tick(symbol)
    if tick is not None:
        out["tick_now"] = {
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "time": int(tick.time),
            "time_msc": int(getattr(tick, "time_msc", 0)),
            "tick_time_minus_utc_s": int(tick.time - now_utc.timestamp()),
            "age_vs_server_clock_s": None,
        }
    else:
        out["tick_now"] = None
    # margin / profit calculations (NOT orders)
    price = None
    if tick is not None and tick.ask > 0:
        price = float(tick.ask)
    elif info is not None and getattr(info, "ask", 0):
        price = float(info.ask)
    calc: dict = {"calc_price": price}
    if price:
        vmin = float(info.volume_min)
        for label, vol in (("lot_1.0", 1.0), ("lot_min", vmin)):
            for side_name, side in (("buy", ORDER_TYPE_BUY), ("sell", ORDER_TYPE_SELL)):
                m = client.order_calc_margin(side, symbol, vol, price)
                calc[f"margin_{label}_{side_name}_acct_ccy"] = None if m is None else float(m)
        tick_size = float(info.trade_tick_size) or float(info.point)
        for label, vol in (("lot_1.0", 1.0), ("lot_min", vmin)):
            p = client.order_calc_profit(ORDER_TYPE_BUY, symbol, vol, price, price + 100 * tick_size)
            calc[f"profit_{label}_buy_plus100ticks_acct_ccy"] = None if p is None else float(p)
        calc["notional_1lot_quote_ccy"] = price * float(info.trade_contract_size)
    out["calc"] = calc
    # rates: M1 (spread distribution, recent) and M5/H1 (session structure)
    m1, out["rates_m1"] = _rates_block(client, symbol, TF_M1, 5000)
    m5, out["rates_m5"] = _rates_block(client, symbol, TF_M5, 12000)
    h1, out["rates_h1"] = _rates_block(client, symbol, TF_H1, 3000)
    out["sessions_m5"] = _session_analysis(m5, 300) if m5 else {}
    out["sessions_h1"] = _session_analysis(h1, 3600) if h1 else {}
    if m1:
        last = m1[-1]
        out["last_m1_bar"] = {
            "server_epoch": int(last["time"]),
            "server_clock": datetime.fromtimestamp(int(last["time"]), UTC).isoformat(),
            "spread_points": float(last["spread"]),
        }
    # live tick sampling (bounded): spread distribution + update cadence
    samples: list[tuple[int, float, float]] = []
    t0 = time.monotonic()
    last_msc = None
    while time.monotonic() - t0 < tick_seconds:
        tk = client.symbol_info_tick(symbol)
        if tk is not None:
            msc = int(getattr(tk, "time_msc", 0)) or int(tk.time) * 1000
            if msc != last_msc:
                samples.append((msc, float(tk.bid), float(tk.ask)))
                last_msc = msc
        time.sleep(0.25)
    point = float(info.point) or 1.0
    spreads_pts = [(a - b) / point for _, b, a in samples if a > 0 and b > 0]
    out["tick_sample"] = {
        "seconds": tick_seconds,
        "n_distinct_ticks": len(samples),
        "spread_points": _dist(spreads_pts),
        "first_msc": samples[0][0] if samples else None,
        "last_msc": samples[-1][0] if samples else None,
        "bid_range": [min(b for _, b, _ in samples), max(b for _, b, _ in samples)] if samples else None,
        "sample_head": [list(s) for s in samples[:5]],
    }
    return out


def _account_block(client) -> dict:
    a = client.account_info()
    t = client.terminal_info()
    return {
        "account": {
            "trade_mode": getattr(a, "trade_mode", None),
            "is_demo": getattr(a, "trade_mode", None) == 0,
            "currency": getattr(a, "currency", None),
            "leverage": getattr(a, "leverage", None),
            "balance": getattr(a, "balance", None),
            "equity": getattr(a, "equity", None),
            "margin": getattr(a, "margin", None),
            "margin_free": getattr(a, "margin_free", None),
            "trade_allowed": getattr(a, "trade_allowed", None),
            "trade_expert": getattr(a, "trade_expert", None),
            "margin_mode": getattr(a, "margin_mode", None),
            "margin_so_mode": getattr(a, "margin_so_mode", None),
            "margin_so_call": getattr(a, "margin_so_call", None),
            "margin_so_so": getattr(a, "margin_so_so", None),
        }
        if a is not None
        else None,
        "terminal": {
            "connected": getattr(t, "connected", None),
            "trade_allowed": getattr(t, "trade_allowed", None),
            "tradeapi_disabled": getattr(t, "tradeapi_disabled", None),
        }
        if t is not None
        else None,
    }


def _worker(mode: str, symbols: list[str], out: Path, tick_seconds: float) -> int:
    connection = MT5Connection(get_real_client())
    result = connection.connect(load_config())
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        client = connection.client
        now = datetime.now(UTC)
        acct = _account_block(client)
        if not (acct["account"] and acct["account"]["is_demo"]):
            print("ABORT: probe only runs against a verified DEMO account")
            return 1
        snap: dict = {"retrieved_at": now.isoformat(), "mode": mode, **acct}
        if mode == "discover":
            raw = client.symbols_get() or ()
            snap["n_symbols_total"] = len(raw)
            rows = []
            for s in raw:
                name, desc, path = str(s.name), str(getattr(s, "description", "")), str(getattr(s, "path", ""))
                hay = f"{name} {desc} {path}"
                kind = "oil" if OIL_RE.search(hay) else ("crypto" if CRYPTO_RE.search(hay) else None)
                if kind:
                    rows.append(
                        {
                            "kind": kind,
                            "name": name,
                            "description": desc,
                            "path": path,
                            "trade_mode": getattr(s, "trade_mode", None),
                            "visible": getattr(s, "visible", None),
                            "digits": getattr(s, "digits", None),
                            "currency_profit": getattr(s, "currency_profit", None),
                            "trade_contract_size": getattr(s, "trade_contract_size", None),
                        }
                    )
            snap["candidates"] = rows
            for r in rows:
                print(f"{r['kind']:<7}{r['name']:<18}{r['path']:<40}{r['description']}")
        else:
            snap["symbols"] = {}
            for sym in symbols:
                snap["symbols"][sym] = _probe_symbol(client, sym, tick_seconds, now)
                print(f"probed {sym}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
        return 0
    finally:
        connection.disconnect()


SNAPSHOT = REPO_ROOT / "research" / "reports" / "v2_markets" / "symbol_snapshot.json"
VERDICT_OUT = REPO_ROOT / "docs" / "evidence" / "phase2_preflight_verdict.json"


def _from_snapshot(symbols: list[str], out: Path, blocked_note: str) -> int:
    """Offline evidence in the `probe` schema from the stored attach-only snapshot (static fields only)."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    found: dict[str, dict] = {}
    for bucket in ("markets", "near_misses"):
        for entry in snap.get(bucket, {}).values():
            for cand in entry.get("candidates", []):
                if cand.get("name") in symbols and cand["name"] not in found:
                    info = {k: v for k, v in cand.items() if k not in ("tier", "score", "tick_bid", "tick_ask", "tick_time_minus_utc_s")}
                    found[cand["name"]] = {
                        "symbol": cand["name"],
                        "symbol_info": info,
                        "tick_now": None,
                        "note": "static snapshot: symbol was not in Market Watch (visible=false), so no quote, margin, rates or session data",
                    }
    doc = {
        "retrieved_at": snap.get("retrieved_at"),
        "mode": "static_snapshot",
        "source": "research/reports/v2_markets/symbol_snapshot.json (read-only attach, symbols_get/symbol_info)",
        "account": {"is_demo": snap.get("broker_account_kind") == "DEMO", "currency": "EUR", "currency_source": "docs/V2_MARKETS.md"},
        "n_symbols_total": snap.get("n_symbols_total"),
        "symbols": found,
        "live_probe": {"status": "BLOCKED", "reason": blocked_note},
    }
    missing = [s for s in symbols if s not in found]
    if missing:
        print(f"not in snapshot: {missing}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    print(f"wrote {out} ({len(found)} symbols)")
    return 1 if missing else 0


def _verdict(probe_path: Path, out: Path) -> int:
    from markets.preflight import run_all

    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    verdicts = run_all(probe)
    doc = {
        "evaluated_at_probe": probe.get("retrieved_at"),
        "probe_mode": probe.get("mode"),
        "verdicts": {m: v.as_dict() for m, v in verdicts.items()},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    for m, v in verdicts.items():
        print(f"{m}: {v.verdict}")
        for r in v.reasons:
            print(f"   - {r}")
    return 0 if all(v.verdict == "GREEN" for v in verdicts.values()) else 1


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--worker"]
    worker = "--worker" in sys.argv[1:]
    if argv and argv[0] == "verdict":
        return _verdict(Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT, Path(argv[2]) if len(argv) > 2 else VERDICT_OUT)
    if argv and argv[0] == "from-snapshot":
        note = (
            "MT5_CONNECTION_BUSY: the live DEMO runner (pid in autotrader_mt5_connection.lock, heartbeat-refreshed) "
            "permanently holds the global MT5 lock; 4 bounded attempts with 10/20/30 s backoff all refused. "
            "Nothing was forced. Re-run `probe Brent,BTCUSD` when the lock is free."
        )
        return _from_snapshot([s for s in argv[1].split(",") if s], Path(argv[2]) if len(argv) > 2 else DEFAULT_OUT, note)
    tick_seconds = 60.0
    if "--tick-seconds" in argv:
        i = argv.index("--tick-seconds")
        tick_seconds = float(argv[i + 1])
        del argv[i : i + 2]
    if not argv or argv[0] not in ("discover", "probe"):
        print(__doc__)
        return 2
    mode = argv[0]
    symbols = [s for s in argv[1].split(",") if s] if mode == "probe" and len(argv) > 1 else []
    rest = argv[2:] if mode == "probe" else argv[1:]
    out = Path(rest[0]) if rest else (DEFAULT_OUT if mode == "probe" else DISCOVER_OUT)
    if mode == "probe" and not symbols:
        print("probe needs exact broker symbols (comma separated)", file=sys.stderr)
        return 2
    if worker:
        return _worker(mode, symbols, out, tick_seconds)
    try:
        config = load_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    timeout = 90.0 + tick_seconds * max(1, len(symbols)) + 60.0
    extra = (mode, ",".join(symbols), str(out), "--tick-seconds", str(tick_seconds))
    if mode == "discover":
        extra = (mode, str(out))
    for attempt in range(1, 5):
        result = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=extra)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if not result.timed_out and result.returncode == 0:
            return 0
        blob = (result.stdout + result.stderr).lower()
        locked = "lock" in blob
        print(f"attempt {attempt}: timed_out={result.timed_out} rc={result.returncode} lock_related={locked}", file=sys.stderr)
        if attempt < 4:
            time.sleep(10 * attempt)
    print("BLOCKED: terminal not attachable / lock held after 4 bounded attempts", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
