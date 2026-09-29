"""Strategy-family registry (research only)."""

from __future__ import annotations

from types import ModuleType

from alpha.breakout import strategy as breakout
from alpha.momentum import strategy as momentum
from alpha.pullback import strategy as pullback

FAMILIES: dict[str, ModuleType] = {"MOMENTUM": momentum, "BREAKOUT": breakout, "PULLBACK": pullback}


def variant_id(family: str, params: dict) -> str:
    return family + "|" + ",".join(f"{k}={params[k]}" for k in sorted(params))
