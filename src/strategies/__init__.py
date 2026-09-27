"""Deterministic strategy families which emit opinions only."""

from strategies.breakout import BreakoutConfig, BreakoutStrategy
from strategies.fusion import SignalFusion
from strategies.momentum import MomentumConfig, MomentumStrategy
from strategies.pullback import PullbackConfig, PullbackStrategy

__all__ = [
    "BreakoutConfig",
    "BreakoutStrategy",
    "MomentumConfig",
    "MomentumStrategy",
    "PullbackConfig",
    "PullbackStrategy",
    "SignalFusion",
]

