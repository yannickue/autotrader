"""Plain-words rendering of a genome for reports (catalog rationale strings).  Research only."""

from __future__ import annotations

import contextlib
from typing import Any

from alpha.discovery.catalog import CATALOG
from alpha.discovery.genome import Clause, Genome


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def describe_clause(c: Clause, resolver: Any = None) -> str:
    e = CATALOG[c.feature]
    if e.kind == "label":
        cond = f"{c.feature} in {{{', '.join(c.labels)}}}"
    elif e.kind == "flag":
        cond = f"{c.feature} is set"
    elif e.kind == "level":
        cond = f"close {c.op} {e.other_feature}"
    elif e.kind == "fixed":
        cond = f"{c.feature} {c.op} {e.fixed_value}"
    else:
        cond = f"{c.feature} {c.op} Train-quantile {c.q:.2f}"
        if resolver is not None and c.q is not None:
            with contextlib.suppress(ValueError):
                cond += f" (= {resolver.value(e.feature, c.q, e.floor):.4g})"
    return f"{cond}  [{e.role}]"


def describe_genome(g: Genome, resolver: Any = None) -> dict[str, Any]:
    """H1 / M15 / M5 trigger / stop / target / window in words (LONG frame; SHORT mirrors)."""
    stop = g.stop
    if stop.kind == "atr_multiple":
        stop_txt = f"stop {stop.multiple:.1f} x M5 ATR from the fill"
    elif stop.kind == "last_swing":
        stop_txt = "stop beyond the last confirmed swing pivot"
    else:
        stop_txt = f"stop {stop.offset:g} pts beyond {stop.level}"
    trig = [describe_clause(c, resolver) for c in g.trigger]
    if g.or_group:
        trig.append("OR( " + "  |  ".join(describe_clause(c, resolver) for c in g.or_group) + " )")
    return {
        "direction": g.direction + (" (mirror of the LONG-frame rules)" if g.direction == "SHORT"
                                    else ""),
        "h1_logic": [describe_clause(c, resolver) for c in g.regime] or ["(no H1 regime filter)"],
        "m15_logic": [describe_clause(c, resolver) for c in g.context] or ["(no M15 filter)"],
        "m5_trigger": trig,
        "stop": stop_txt,
        "target": f"fixed target {g.target_r:g} R, next-bar-open fill, forced flat 21:30 Berlin",
        "time_window": ("all entry hours" if g.time_window is None
                        else f"entries {_hhmm(g.time_window[0])}-{_hhmm(g.time_window[1])} Berlin"),
    }


__all__ = ("describe_clause", "describe_genome")
