"""Capital-growth Monte-Carlo simulator (research only).

Scales a GIVEN empirical R-multiple stream by a risk schedule. It never creates edge: a zero- or
negative-expectancy stream decays whatever the risk fraction. See docs/V2_GROWTH.md.
"""

from alpha.growth.analytics import growth_rate, kelly_fraction, required_fraction
from alpha.growth.engine import SimConfig, SimResult, simulate, summarize
from alpha.growth.schedules import (
    DrawdownThrottle,
    FixedFraction,
    FractionalKelly,
    RampAfterWins,
    Schedule,
)
from alpha.growth.sizing import LotSizing, lot_sizing_for
from alpha.growth.stream import RStream, load_stream, synthetic_stream

__all__ = [
    "DrawdownThrottle",
    "FixedFraction",
    "FractionalKelly",
    "LotSizing",
    "RStream",
    "RampAfterWins",
    "Schedule",
    "SimConfig",
    "SimResult",
    "growth_rate",
    "kelly_fraction",
    "load_stream",
    "lot_sizing_for",
    "required_fraction",
    "simulate",
    "summarize",
    "synthetic_stream",
]
