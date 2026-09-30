# ruff: noqa: E501
"""MAP-Elites archive: one elite per niche, twins, novelty, niche keys."""

from __future__ import annotations

import numpy as np

from alpha.discovery import temporal_genome as tg
from alpha.discovery.temporal_archetypes import random_genome
from alpha.discovery.temporal_niches import (
    Elite,
    NicheArchive,
    NicheKey,
    freq_bucket,
    niche_key,
    role_path,
)
from alpha.temporal import spec as sp

K1 = NicheKey("TREND>BOS", "LONG", "H1+M5", 1)
K2 = NicheKey("ZONE>SWEEP", "SHORT", "M15", 2)


def _e(key, chash, fit, twin=""):
    return Elite(key, chash, fit, twin)


def test_role_path_and_key_of_the_section5_template():
    g = tg.TemporalGenome(
        "SHORT",
        (sp.Clause("state", "TREND_UP", "H1"), sp.Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")),
        tuple(tg.StepGene(e, 5) for e in (tg.EventGene("SWEEP_LOW", "M5", "prior20"),
                                          tg.EventGene("RECLAIM_UP", "M5", "k3"),
                                          tg.EventGene("BOS_UP", "M5"),
                                          tg.EventGene("RETEST_HOLD_UP", "M5"))),
        expires_after=24, stop=tg.StopGene("atr", "", 0.0, 2.0, 3.0), target=tg.TargetGene("fixed_r", 2.0))
    assert role_path(g) == "TREND+ZONE>SWEEP>RECLAIM>BOS>RETEST"
    k = niche_key(g, 0.3)
    assert k == NicheKey("TREND+ZONE>SWEEP>RECLAIM>BOS>RETEST", "SHORT", "M5+M15+H1", 2)
    pb = tg.TemporalGenome(
        "LONG", (sp.Clause("state", "TREND_UP", "H1"),),
        (tg.StepGene(tg.EventGene("TREND_UP", "M15", op="HOLD", arg=5), 5), tg.StepGene(tg.EventGene("MOMENTUM_RESUME_UP", "M5"), 5)),
        expires_after=24, stop=tg.StopGene("atr", "", 0.0, 2.0, 3.0), target=tg.TargetGene("fixed_r", 2.0))
    assert role_path(pb) == "TREND>PULLBACK>MOMENTUM"


def test_freq_buckets():
    assert [freq_bucket(x) for x in (None, 0.0, 0.049, 0.05, 0.19, 0.2, 0.59, 0.6, 5.0)] == [0, 0, 0, 1, 1, 2, 2, 3, 3]


def test_one_elite_per_niche_best_wins_tie_by_hash():
    a = NicheArchive()
    assert a.insert(_e(K1, "b", 0.5)) == "new_niche"
    assert a.insert(_e(K1, "c", 0.4)) == "rejected"
    assert a.insert(_e(K1, "a", 0.5)) == "improved"  # equal fitness, smaller hash wins
    assert a.insert(_e(K1, "d", 0.9)) == "improved"
    assert a.insert(_e(K1, "d", 0.9)) == "kept"
    assert a.n_niches == 1 and a.occupancy(K1) == 1 and a.elites()[0].chash == "d"
    a.insert(_e(K2, "z", -1.0))
    assert a.n_niches == 2 and [e.chash for e in a.elites()] == ["d", "z"]


def test_per_niche_cap_keeps_top_n():
    a = NicheArchive(per_niche=3)
    for i, f in enumerate([0.1, 0.5, 0.3, 0.9, 0.2]):
        a.insert(_e(K1, f"h{i}", f))
    assert a.occupancy(K1) == 3
    assert [e.fitness for e in a.elites()] == [0.9, 0.5, 0.3]


def test_behavioural_twin_only_best_keeps_slot():
    a = NicheArchive()
    assert a.insert(_e(K1, "x", 0.5, twin="T")) == "new_niche"
    assert a.insert(_e(K2, "y", 0.4, twin="T")) == "twin"  # same TRAIN decision stream, worse
    assert a.twins_rejected == 1 and a.find("y") is None
    assert a.insert(_e(K2, "w", 0.7, twin="T")) == "new_niche"  # better twin displaces the owner
    assert a.find("x") is None and a.twins_replaced == 1 and a.twin_owner["T"] == "w"
    assert a.n_niches == 1 and a.occupancy(K1) == 0


def test_twin_displacement_is_atomic_when_the_new_niche_is_full():
    a = NicheArchive()
    a.insert(_e(K1, "x", 0.2, twin="T"))
    a.insert(_e(K2, "best", 0.9))
    # candidate is a better twin of x but its own niche (K2) is held by a better elite: rejected, x stays
    assert a.insert(_e(K2, "t", 0.5, twin="T")) == "rejected"
    assert a.find("x") is not None and a.twins_replaced == 0


def test_novelty_bonus_decays_with_visits():
    a = NicheArchive()
    fresh = a.novelty(K1, 0.1)
    assert fresh == 0.1
    for _ in range(8):
        a.visit(K1)
    assert a.novelty(K1, 0.1) < fresh / 2.9 and a.novelty(K2, 0.1) == 0.1
    assert a.novelty(K1) > 0


def test_random_genomes_spread_over_many_niches():
    rng = np.random.default_rng(3)
    a = NicheArchive()
    for _ in range(300):
        g = random_genome(rng, tg.EventPool.full())
        a.insert(_e(niche_key(g, float(rng.uniform(0, 1))), tg.canonical_hash(g), float(rng.normal())))
    assert a.n_niches > 40 and all(a.occupancy(k) == 1 for k in a.cells)
