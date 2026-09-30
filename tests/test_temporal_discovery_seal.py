# ruff: noqa: E501
"""Structural seal (no Validation in search/fitness) + documented non-feasibility of a V1 parity test."""

from __future__ import annotations

import dataclasses
import importlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery import temporal_genome as tg
from alpha.discovery.evaluate import TrainView
from alpha.discovery.temporal_archetypes import random_genome
from alpha.temporal import spec as sp

SEARCH_SIDE = ("temporal_genome", "temporal_compile", "temporal_archetypes", "temporal_search", "temporal_niches")
TOKENS = ("validation_gate_view", "_validation", "ValidationView", "temporal_validation_gate_view",
          "OosGate", "split.validation", "SplitPlan")


@pytest.mark.parametrize("name", SEARCH_SIDE)
def test_no_validation_access_in_search_side_modules(name):
    src = Path(importlib.import_module(f"alpha.discovery.{name}").__file__).read_text(encoding="utf-8").lower()
    for token in TOKENS:
        assert token.lower() not in src, (name, token)
    assert "temporal_evaluate" not in src  # the search side never imports the sealed evaluator


def test_fitness_reads_only_the_train_view():
    assert {f.name for f in dataclasses.fields(TrainView)} == {"genome_hash", "complexity", "base", "adverse"}
    src = Path(importlib.import_module("alpha.discovery.fitness").__file__).read_text(encoding="utf-8")
    assert "ValidationView" not in src.replace("Validation partition is not reachable", "")


def test_evaluate_module_exposes_validation_only_via_the_gate():
    src = Path(importlib.import_module("alpha.discovery.temporal_evaluate").__file__).read_text(encoding="utf-8")
    assert src.count("def temporal_validation_gate_view") == 1
    assert "train_fitness(" not in src  # fitness lives on the search side and only ever sees ``.train``


def test_v1_parity_is_not_feasible_without_editing_existing_files():
    """An anchor-only temporal spec cannot exist, hence no bit-exact V1 ``evaluate_spec`` parity test.

    * ``spec.validate`` (frozen) demands 1..5 transitions and ``min_gap == 1`` (no same-bar chaining): the
      earliest temporal decision is anchor bar + 1, V1 decides ON the rule bar.
    * V1 rules read the FeatureSet with its own catalogue / TRAIN resolver; a MarketFrame carries 5 features
      and named event arrays only, and V1 stop kinds (session_level, atr_multiple ...) do not exist in V2.
    Both facts are pinned here; parity has to be re-decided once the real-EventSet frame adapter exists.
    """
    g = random_genome(np.random.default_rng(1), tg.EventPool.full(), direction="LONG")
    with pytest.raises(tg.GenomeError):
        tg.validate(replace(g, steps=()))
    long_spec = __import__("alpha.discovery.temporal_compile", fromlist=["x"]).compile_temporal(replace(g, direction="LONG"))
    with pytest.raises(ValueError):
        replace(long_spec, states=())
    with pytest.raises(ValueError):
        sp.Transition(long_spec.states[0].trigger, min_gap=0)
