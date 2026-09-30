"""V2 symbol discovery: READ-ONLY, attach-only MT5 DEMO snapshot for the five V2 markets.

Calls only `symbols_get`, `symbol_info`, `symbol_info_tick` (no orders, no position changes,
no `symbol_select`, no login/account switching) inside a hard-timeout child process. Uses the
existing 4-tier matcher (`instruments.discovery`). Nothing is guessed: a market is `resolved`
only when the matcher returns MATCHED with a non-dated, non-CFD-future symbol; everything else is
reported with its candidate list for human review.

Usage:  <PY> scripts/v2_discover_symbols.py [OUT_JSON]
Default OUT_JSON: research/reports/v2_markets/symbol_snapshot.json
If the terminal is not attachable within the bounded timeout the snapshot records
`terminal_attachable: false` and the script exits 1 (no retry loop).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v2_common import REPO_ROOT, load_config

from adapters.activtrades_mt5.bounded import (
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.connection import MT5Connection
from adapters.activtrades_mt5.real_client import get_real_client
from adapters.config import MT5ConfigError
from instruments.discovery import (
    BrokerSymbolCandidate,
    CanonicalInstrument,
    match_symbols,
)

DEFAULT_OUT = REPO_ROOT / "research" / "reports" / "v2_markets" / "symbol_snapshot.json"

# Aliases are configuration data (per discovery.py's design), not confirmed broker names.
TARGETS: dict[str, tuple[str, ...]] = {
    "GER40": ("GER40", "DAX", "DE40", "DAX40", "GERMANY 40"),
    "NAS100": ("NAS100", "NASDAQ", "US100", "USTEC", "NASDAQ 100"),
    "SPX500": ("SPX500", "SPX", "US500", "SP500", "S&P 500"),
    "XAUUSD": ("XAUUSD", "GOLD"),
    "EURUSD": ("EURUSD",),
}
NEAR_MISSES: dict[str, tuple[str, ...]] = {
    "US30": ("US30", "DJ30", "DOW", "DJIA"),
    "GBPUSD": ("GBPUSD",),
    "EURJPY": ("EURJPY",),
    "UK100": ("UK100", "FTSE"),
    "OIL": ("OIL", "WTI", "BRENT", "XTIUSD", "XBRUSD"),
}
# Dated futures-style suffix (e.g. Ger40Dec26): never resolved automatically.
DATED_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2}$", re.IGNORECASE)

# Human-reviewed verification rules (observed 2026-09-30: the alias matcher alone mis-ranks, e.g.
# NAS100 -> dated future, SPX500 -> "Plus500 Ltd"). A symbol is `resolved` ONLY if EXACTLY ONE
# symbol satisfies path prefix + description regex; otherwise fail closed (needs review).
RESOLVE_RULES: dict[str, tuple[str, str]] = {
    "GER40": (r"^Cash Indices\\", r"^DAX Cash Index$"),
    "NAS100": (r"^Cash Indices\\", r"^(US Tech 100|NASDAQ 100) Cash Index$"),
    "SPX500": (r"^Cash Indices\\", r"^(SP|S&P) 500 Cash Index$"),
    "XAUUSD": (r"^Metals\\", r"^Gold$"),
    "EURUSD": (r"^Forex\\Majors\\", r"^Euro vs US Dollar$"),
}

INFO_FIELDS = (
    "name",
    "description",
    "path",
    "digits",
    "point",
    "trade_tick_size",
    "trade_tick_value",
    "trade_tick_value_profit",
    "trade_tick_value_loss",
    "trade_contract_size",
    "volume_min",
    "volume_step",
    "volume_max",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "trade_mode",
    "trade_calc_mode",
    "trade_stops_level",
    "trade_freeze_level",
    "swap_mode",
    "swap_long",
    "swap_short",
    "margin_initial",
    "margin_maintenance",
    "spread",
    "spread_float",
    "session_deals",
    "session_quotes",
    "visible",
    "select",
)


def _info_dict(info: object) -> dict:
    out = {}
    for f in INFO_FIELDS:
        if hasattr(info, f):
            v = getattr(info, f)
            out[f] = v if isinstance(v, (int, float, str, bool)) or v is None else str(v)
    return out


def _discover(out: Path) -> int:
    connection = MT5Connection(get_real_client())
    result = connection.connect(load_config())
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        client = connection.client
        account = client.account_info()
        kind = "DEMO" if getattr(account, "trade_mode", None) == 0 else "NOT_DEMO"
        if kind != "DEMO":
            print("ABORT: discovery only runs against a verified DEMO account")
            return 1
        raw = client.symbols_get() or ()
        by_name = {str(s.name): s for s in raw}
        cands = [
            BrokerSymbolCandidate(
                broker_symbol=str(s.name), description=str(getattr(s, "description", ""))
            )
            for s in raw
        ]
        now = datetime.now(UTC)
        snapshot: dict = {
            "retrieved_at": now.isoformat(),
            "terminal_attachable": True,
            "broker_account_kind": kind,
            "n_symbols_total": len(raw),
            "path_groups": {},
            "markets": {},
            "near_misses": {},
        }
        groups: dict[str, int] = {}
        for s in raw:
            top = str(getattr(s, "path", "")).split("\\")[0]
            groups[top] = groups.get(top, 0) + 1
        snapshot["path_groups"] = dict(sorted(groups.items()))

        def _entry(canonical: str, aliases: tuple[str, ...], match) -> dict:
            ranked = []
            for r in match.candidates[:6]:
                info = by_name.get(r.broker_symbol)
                tick = client.symbol_info_tick(r.broker_symbol)
                d = {
                    "tier": r.tier.value,
                    "score": round(r.score, 3),
                    **(_info_dict(info) if info is not None else {}),
                }
                if tick is not None:
                    d["tick_bid"] = float(tick.bid)
                    d["tick_ask"] = float(tick.ask)
                    d["tick_time_minus_utc_s"] = int(tick.time - now.timestamp())
                ranked.append(d)
            resolved = None
            rule = RESOLVE_RULES.get(canonical)
            if rule is not None:
                hits = [
                    n
                    for n, i in by_name.items()
                    if re.search(rule[0], str(getattr(i, "path", "")))
                    and re.search(rule[1], str(getattr(i, "description", "")).strip())
                    and not DATED_RE.search(n)
                ]
                if len(hits) == 1:
                    resolved = hits[0]
                    note = (
                        f"unique rule hit; matcher said {match.broker_symbol} "
                        f"({match.status.value})"
                    )
                else:
                    note = f"rule hits={hits}; NOT resolved (fail closed)"
            else:
                note = "near-miss listing only (not resolved)"
            resolved_info = None
            if resolved is not None:
                resolved_info = _info_dict(by_name[resolved])
                rt = client.symbol_info_tick(resolved)
                if rt is not None:
                    resolved_info["tick_bid"] = float(rt.bid)
                    resolved_info["tick_ask"] = float(rt.ask)
                    resolved_info["tick_time_minus_utc_s"] = int(rt.time - now.timestamp())
                # Read-only margin CALCULATION (not an order): 1 lot BUY at current ask.
                price = float(rt.ask) if rt is not None and rt.ask > 0 else None
                if price is None:
                    ri = by_name[resolved]
                    price = float(getattr(ri, "bid", 0) or 0) or None
                if price:
                    m = client.order_calc_margin(0, resolved, 1.0, price)
                    resolved_info["margin_1lot_account_ccy"] = None if m is None else float(m)
                    resolved_info["calc_price"] = price
                    resolved_info["notional_1lot_quote_ccy"] = price * float(
                        by_name[resolved].trade_contract_size
                    )
            return {
                "resolved_info": resolved_info,
                "aliases": list(aliases),
                "status": match.status.value,
                "resolved_broker_symbol": resolved,
                "note": note,
                "candidates": ranked,
            }

        for group, bucket in ((TARGETS, "markets"), (NEAR_MISSES, "near_misses")):
            instruments = [
                CanonicalInstrument(canonical_symbol=c, aliases=a) for c, a in group.items()
            ]
            matches = match_symbols(cands, instruments)
            for canonical, aliases in group.items():
                snapshot[bucket][canonical] = _entry(canonical, aliases, matches[canonical])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot, indent=1, default=str), encoding="utf-8")
        for k, v in snapshot["markets"].items():
            print(f"{k}: {v['status']} -> {v['resolved_broker_symbol']} ({v['note']})")
        for k, v in snapshot["near_misses"].items():
            print(f"near {k}: {v['status']} -> {v['resolved_broker_symbol']}")
        return 0
    finally:
        connection.disconnect()


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--worker"]
    out = Path(argv[0]) if argv else DEFAULT_OUT
    if "--worker" in sys.argv[1:]:
        return _discover(out)
    try:
        config = load_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    timeout = default_staged_timeout_seconds() * 2
    result = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=(str(out),))
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out or result.returncode != 0:
        reason = "terminal not attachable (timeout)" if result.timed_out else "worker failed"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_text(
                json.dumps(
                    {
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "terminal_attachable": False,
                        "reason": reason,
                    },
                    indent=1,
                ),
                encoding="utf-8",
            )
        print(reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
