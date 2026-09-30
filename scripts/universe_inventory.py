# ruff: noqa: E501
"""Lane U: READ-ONLY broker universe inventory (MT5 DEMO) + offline classification / shadow-spec generation.

MT5 calls used (inside the hard-timeout bounded child; nothing else): account_info, terminal_info, symbols_get,
symbol_select (Market Watch ONLY, for quote access; every selected name is recorded), symbol_info,
symbol_info_tick, order_calc_margin (CALCULATION, not an order), copy_rates_from_pos.
NEVER order_send / order_check / login.

Usage:
    <PY> scripts/universe_inventory.py list  [OUT_JSON]              # all broker symbols, compact
    <PY> scripts/universe_inventory.py scan  [LIST_JSON] [OUT_JSON]  # rule-based selection + deep read-only probe
    <PY> scripts/universe_inventory.py restore                        # deselect Market Watch symbols not visible in the list snapshot
    <PY> scripts/universe_inventory.py build [RAW_JSON]              # OFFLINE: inventory json/md + shadow TOMLs
Selection rule: src/instruments/universe.py::select_candidates (pure, deterministic, documented there).
"""

from __future__ import annotations

import itertools
import json
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

EVID = REPO_ROOT / "docs" / "evidence"
LIST_OUT = EVID / "universe_symbol_list.json"
RAW_OUT = EVID / "universe_probe_raw.json"
TF_M1, TF_M5 = 1, 5
BUY, SELL = 0, 1


def _plain(v):
    return v if isinstance(v, (int, float, str, bool)) or v is None else str(v)


def _info(info) -> dict:
    if info is None:
        return {}
    if hasattr(info, "_asdict"):
        return {k: _plain(v) for k, v in info._asdict().items()}
    return {f: _plain(getattr(info, f)) for f in dir(info) if not f.startswith("_") and not callable(getattr(info, f))}


def _pct(s: list[float], p: float):
    if not s:
        return None
    return s[min(len(s) - 1, max(0, round(p / 100.0 * (len(s) - 1))))]


def _rates(client, sym: str, tf: int, n: int):
    r = client.copy_rates_from_pos(sym, tf, 0, n)
    if r is None or len(r) == 0:
        return [], {"available": False, "n": 0}
    t = [int(x["time"]) for x in r]
    sp = sorted(float(x["spread"]) for x in r)
    cl = [float(x["close"]) for x in r]
    return list(r), {
        "available": True,
        "n": len(t),
        "first_bar_server_epoch": t[0],
        "last_bar_server_epoch": t[-1],
        "spread_points": {"median": _pct(sp, 50), "p95": _pct(sp, 95), "max": sp[-1]},
        "last_close": cl[-1],
    }


def _sessions(rates: list) -> dict:
    """Observed session structure from bar timestamps (server wall clock encoded as epoch)."""
    times = [int(r["time"]) for r in rates]
    gaps: dict[str, int] = {}
    for a, b in itertools.pairwise(times):
        if b - a > 15 * 60:
            da, db = datetime.fromtimestamp(a, UTC), datetime.fromtimestamp(b, UTC)
            key = f"wd{da.weekday()} {da.strftime('%H:%M')} -> wd{db.weekday()} {db.strftime('%H:%M')} ({(b - a) // 60}min)"
            gaps[key] = gaps.get(key, 0) + 1
    per_day: dict[str, list[int]] = {}
    for t in times:
        d = datetime.fromtimestamp(t, UTC)
        per_day.setdefault(d.strftime("%Y-%m-%d"), []).append(d.hour * 60 + d.minute)
    wd_first: dict[int, list[int]] = {}
    wd_last: dict[int, list[int]] = {}
    for day, mins in per_day.items():
        wd = datetime.strptime(day, "%Y-%m-%d").weekday()
        wd_first.setdefault(wd, []).append(min(mins))
        wd_last.setdefault(wd, []).append(max(mins))

    def med(v: list[int]) -> int:
        return sorted(v)[len(v) // 2]

    return {
        "gap_patterns_server_clock": dict(sorted(gaps.items(), key=lambda kv: -kv[1])[:12]),
        "weekday_median_first_last_minute_server": {
            str(k): [med(wd_first[k]), med(wd_last[k]), len(wd_first[k])] for k in sorted(wd_first)
        },
        "weekdays_with_bars": sorted(wd_first),
    }


def _probe(client, sym: str, now: datetime) -> dict:
    out: dict = {"symbol": sym}
    selected_by_us = False
    info = client.symbol_info(sym)
    if info is not None and not bool(getattr(info, "visible", False)):
        selected_by_us = bool(client.symbol_select(sym, True))
        out["selected_into_market_watch"] = selected_by_us
        time.sleep(0.4)
        info = client.symbol_info(sym)
    out["symbol_info"] = _info(info)
    if info is None:
        out["error"] = "symbol_info None"
        return out
    tick = None
    for _ in range(6):  # bounded wait for a first quote on freshly selected symbols
        tick = client.symbol_info_tick(sym)
        if tick is not None and int(tick.time) > 0:
            break
        time.sleep(0.4)
    if tick is not None:
        out["tick_now"] = {
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "time": int(tick.time),
            "tick_time_minus_utc_s": int(tick.time - now.timestamp()),
        }
    else:
        out["tick_now"] = None
    _m1, out["rates_m1"] = _rates(client, sym, TF_M1, 3000)
    m5, out["rates_m5"] = _rates(client, sym, TF_M5, 12000)
    out["sessions_m5"] = _sessions(m5) if m5 else {}
    price, price_src = None, None
    if tick is not None and tick.ask > 0:
        price, price_src = float(tick.ask), "tick_ask"
    elif getattr(info, "ask", 0):
        price, price_src = float(info.ask), "symbol_info_ask"
    elif m5:
        price, price_src = float(m5[-1]["close"]), "last_m5_close"  # margin CALCULATION only (market closed, no tick)
    calc: dict = {"calc_price": price, "calc_price_source": price_src}
    if price:
        vmin = float(info.volume_min)
        for side_name, side in (("buy", BUY), ("sell", SELL)):
            m = client.order_calc_margin(side, sym, vmin, price)
            calc[f"margin_lot_min_{side_name}_acct_ccy"] = None if m is None else float(m)
        calc["notional_1lot_quote_ccy"] = price * float(info.trade_contract_size)
    out["calc"] = calc
    if selected_by_us:  # restore the Market Watch state we found
        out["deselected_again"] = bool(client.symbol_select(sym, False))
    return out


def _worker(mode: str, list_json: Path, out: Path) -> int:
    connection = MT5Connection(get_real_client())
    res = connection.connect(load_config())
    if not res.success:
        print(f"CONNECT: FAIL -- {res.reason}")
        return 1
    try:
        client = connection.client
        acct = client.account_info()
        if acct is None or getattr(acct, "trade_mode", None) != 0:
            print("ABORT: inventory only runs against a verified DEMO account")
            return 1
        now = datetime.now(UTC)
        snap: dict = {
            "retrieved_at": now.isoformat(),
            "account_currency": getattr(acct, "currency", None),
            "account_leverage": getattr(acct, "leverage", None),
        }
        if mode == "restore":
            orig = {r["name"] for r in json.loads(list_json.read_text(encoding="utf-8"))["symbols"] if r["visible"]}
            restored, failed = [], []
            for s in client.symbols_get() or ():
                if bool(getattr(s, "visible", False)) and str(s.name) not in orig:
                    (restored if client.symbol_select(str(s.name), False) else failed).append(str(s.name))
            print(f"restore: deselected {len(restored)}, failed {failed}")
            snap["restored"], snap["failed"] = restored, failed
        elif mode == "list":
            raw = client.symbols_get() or ()
            rows = [
                {
                    "name": str(s.name),
                    "description": str(getattr(s, "description", "")),
                    "path": str(getattr(s, "path", "")),
                    "trade_mode": getattr(s, "trade_mode", None),
                    "visible": bool(getattr(s, "visible", False)),
                    "digits": getattr(s, "digits", None),
                    "currency_profit": getattr(s, "currency_profit", None),
                    "currency_base": getattr(s, "currency_base", None),
                    "trade_contract_size": getattr(s, "trade_contract_size", None),
                }
                for s in raw
            ]
            snap["n_symbols_total"] = len(rows)
            snap["symbols"] = rows
            print(f"listed {len(rows)} symbols")
        else:
            listing = json.loads(list_json.read_text(encoding="utf-8"))["symbols"]
            from instruments.universe import select_candidates

            cands = select_candidates(listing)
            snap["selection"] = [{"name": c.name, "cluster": c.cluster, "rule": c.rule} for c in cands]
            snap["probes"] = {}
            for c in cands:
                try:
                    snap["probes"][c.name] = _probe(client, c.name, datetime.now(UTC))
                except Exception as exc:  # one bad symbol must not kill the scan
                    snap["probes"][c.name] = {"symbol": c.name, "error": f"{type(exc).__name__}: {exc}"}
                print(f"probed {c.name}", flush=True)
            snap["selected_into_market_watch"] = sorted(
                n for n, p in snap["probes"].items() if p.get("selected_into_market_watch")
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
        return 0
    finally:
        connection.disconnect()


def _run_bounded(mode: str, list_json: Path, out: Path, timeout: float) -> int:
    try:
        config = load_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    for attempt in range(1, 6):
        r = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=(mode, str(list_json), str(out)))
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        if not r.timed_out and r.returncode == 0:
            return 0
        print(f"attempt {attempt}: timed_out={r.timed_out} rc={r.returncode}", file=sys.stderr)
        if attempt < 5:
            time.sleep(10 * attempt)
    print("BLOCKED: terminal not attachable / MT5 lock held after 5 bounded attempts", file=sys.stderr)
    return 1


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--worker"]
    worker = "--worker" in sys.argv[1:]
    if not argv or argv[0] not in ("list", "scan", "build", "restore"):
        print(__doc__)
        return 2
    mode = argv[0]
    if mode == "build":
        from instruments.universe_build import build_outputs

        return build_outputs(Path(argv[1]) if len(argv) > 1 else RAW_OUT, REPO_ROOT)
    if worker:
        return _worker(mode, Path(argv[1]), Path(argv[2]))
    if mode == "restore":
        lst, out = LIST_OUT, EVID / "universe_restore_log.json"
    elif mode == "list":
        lst, out = LIST_OUT, (Path(argv[1]) if len(argv) > 1 else LIST_OUT)
    else:
        lst = Path(argv[1]) if len(argv) > 1 else LIST_OUT
        out = Path(argv[2]) if len(argv) > 2 else RAW_OUT
    return _run_bounded(mode, lst, out, timeout=180.0 if mode in ("list", "restore") else 900.0)


if __name__ == "__main__":
    sys.exit(main())
