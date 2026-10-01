# ruff: noqa: E501
"""REGRESSION GATE: the real observer groups pass the strengthened look-ahead harness (all ObserverBars arrays perturbed after i).

Companion of test_negative_control_leaky.py (which proves the harness has teeth).
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from _leak_harness import assert_no_future_dependence
from test_levels_support import random_walk

from market_observer import acceptance as A
from market_observer import balance as B
from market_observer import levels as L
from market_observer import observer as O
from market_observer import participation as P
from market_observer import schema as S
from market_observer import swings as W

CFG = O.OBSERVER_CONFIG.with_round_steps(1.0, 5.0)
LCFG = L.LevelConfig(round_major_step=5.0, round_minor_step=1.0)
SAMPLE = (14, 30, 99, 100, 101, 149, 150, 151, 233, 234, 235, 330, 361)  # cheap groups: dense, incl. segment break (150) and day boundaries
HEAVY_SAMPLE = (101, 150, 234, 361)  # levels / orchestrator replay from bar 0 per call: fewer decision bars, scale+nan modes only
HEAVY = {"levels", "orchestrator_long", "orchestrator_short"}


@pytest.fixture(scope="module")
def world():
    b = random_walk(400, 11, bars_per_day=100, segment_breaks=(150,))
    rng = np.random.default_rng(2)
    return dataclasses.replace(b, tick_volume=rng.uniform(10, 100, len(b)), spread=rng.uniform(0.01, 0.05, len(b)))


def _ev(bars, i, d=1):
    return O.ObservedEvent(d, float(bars.c[i]), family="STRUCT", variant="breakout")


def _lvl(b, i):
    p = float(b.c[max(i - 5, 0)])
    return S.LevelRef("W", p, p - 0.1, p + 0.1, ("SWING_M5",), 0, int(b.ts_ns[0]))


GROUPS = {
    "levels": lambda b, i: L.build_level_context(b, i, LCFG),
    "swings": lambda b, i: W.swing_features(b, i),
    "balance": lambda b, i: B.balance_features(b, i),
    "participation": lambda b, i: P.participation_features(b, i),
    "acceptance_long": lambda b, i: A.acceptance_features(b, i, _lvl(b, i), 1),
    "acceptance_short": lambda b, i: A.acceptance_features(b, i, _lvl(b, i), -1),
    "orchestrator_long": lambda b, i: O.observe_event(b, i, _ev(b, i, 1), CFG),
    "orchestrator_short": lambda b, i: O.observe_event(b, i, _ev(b, i, -1), CFG),
}


@pytest.mark.parametrize("name", sorted(GROUPS))
def test_real_group_has_no_lookahead_through_any_observerbars_field(world, name):
    if name in HEAVY:
        assert_no_future_dependence(GROUPS[name], world, HEAVY_SAMPLE if name != "orchestrator_short" else HEAVY_SAMPLE[:2], seed=7, modes=("scale", "nan"))
    else:
        assert_no_future_dependence(GROUPS[name], world, SAMPLE, seed=7)
