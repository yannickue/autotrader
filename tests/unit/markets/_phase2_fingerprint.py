# ruff: noqa: E501
"""Fingerprint of everything the five pre-Phase-2 markets expose (bit-identical regression guard)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from alpha.common.market_costs import derived_table
from demo.execution.market_config import load_demo_market_specs
from demo.execution.risk_policy import CLUSTERS, cluster_of
from markets.spec import CANONICALS, DEFAULT_CONFIG_DIR, load_market_spec
from nautilus_mt5.symbols import default_registry, demo_registry, research_registry

FIVE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")


def fingerprint() -> str:
    parts: list[str] = []
    for name in FIVE:
        raw = Path(DEFAULT_CONFIG_DIR / f"{name}.toml").read_bytes()
        raw = raw.replace(bytes([13, 10]), bytes([10]))  # CRLF checkouts hash like LF ones
        parts.append(f"toml:{name}:{hashlib.sha256(raw).hexdigest()}")
    specs = {n: load_market_spec(n) for n in FIVE}
    parts.append("specs:" + repr(specs))
    parts.append("demo:" + repr(load_demo_market_specs()))
    parts.append("clusters:" + repr(dict(CLUSTERS)) + repr({m: cluster_of(m) for m in FIVE}))
    for label, reg in (("default", default_registry()), ("demo", demo_registry()), ("research", research_registry())):
        parts.append(f"reg:{label}:" + repr(reg.all()))
    parts.append("canon:" + repr(tuple(CANONICALS)))
    parts.append("costs:" + derived_table(specs))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
