"""Frozen production spec: sealed hash, immutability, holdout rule, determinism, no MT5."""

from __future__ import annotations

import copy
import json
import sys
from dataclasses import FrozenInstanceError

import pytest

from demo.opportunity import production_spec as ps
from demo.opportunity.production_spec import (
    DEFAULT_PATH,
    ProductionSpecError,
    fit_production_specs,
    from_payload,
    load_production_spec,
    select_specs,
)


def _payload():
    return json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))


def _reseal(p):
    p["strategy_hash"] = ps._hash_body({k: v for k, v in p.items() if k != "strategy_hash"})
    return p


def test_shipped_file_loads_and_covers_all_markets(prod):
    assert set(prod.market_names()) == {"GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"}
    for m in prod.market_names():
        specs = prod.specs_for(m)
        assert len(specs) >= 12
        assert all(fs.thr.ok for fs in specs)
        families = {fs.family for fs in specs}
        assert {"ORB", "GAP", "OVERNIGHT", "VOLREV", "ROUND", "EOD"} <= families
        assert ("LEADLAG" in families) == (m in ("GER40", "NAS100", "SPX500"))
    assert prod.fit_end < "2026-09-01"


def test_hash_seals_the_file():
    good = _payload()
    from_payload(good)
    for mutate in (
        lambda p: p["markets"]["GER40"][0]["spec"].__setitem__("range_min", 30),
        lambda p: p["markets"]["GER40"][2]["thr"].__setitem__(0, 0.001),
        lambda p: p.__setitem__("fit_end", "2026-05-31"),
    ):
        bad = copy.deepcopy(good)
        mutate(bad)
        with pytest.raises(ProductionSpecError, match="strategy_hash mismatch"):
            from_payload(bad)


def test_payload_reaching_into_the_holdout_is_refused():
    p = _payload()
    p["fit_end"] = "2026-09-01"
    with pytest.raises(ProductionSpecError, match="holdout"):
        from_payload(_reseal(p))
    q = _payload()
    q["provenance"]["GER40"]["last_berlin_date"] = "2026-07-15"
    with pytest.raises(ProductionSpecError, match="after fit_end"):
        from_payload(_reseal(q))


def test_fit_end_after_dev_end_is_refused_before_loading_data():
    with pytest.raises(ProductionSpecError, match="development end"):
        fit_production_specs("2026-09-01")


def test_objects_are_immutable(prod):
    fs = prod.specs_for("GER40")[0]
    with pytest.raises(FrozenInstanceError):
        fs.thr_values = (1.0,)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        prod.strategy_hash = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        fs.spec.range_min = 5  # type: ignore[misc,attr-defined]
    assert isinstance(prod.markets, tuple) and isinstance(prod.specs_for("GER40"), tuple)


def test_selection_rule_is_fixed_and_not_performance_driven():
    a, b = select_specs("GER40"), select_specs("GER40")
    assert [s.canonical_hash() for s in a] == [s.canonical_hash() for s in b]
    fams = [s.FAMILY for s in a]
    assert fams[:12] == ["ORB", "ORB", "GAP", "GAP", "OVERNIGHT", "OVERNIGHT", "VOLREV", "VOLREV",
                         "ROUND", "ROUND", "EOD", "EOD"]
    assert fams[12:] == ["LEADLAG", "LEADLAG"]
    assert select_specs("XAUUSD")[-1].FAMILY == "EOD"
    # default + mode complement, nothing else
    assert {s.mode for s in a if s.FAMILY == "ORB"} == {"breakout", "fade"}  # type: ignore[attr-defined]


def test_refit_reproduces_the_shipped_thresholds():
    fresh = fit_production_specs(markets=("EURUSD",))
    shipped = _payload()
    assert fresh["markets"]["EURUSD"] == shipped["markets"]["EURUSD"]
    assert fresh["provenance"]["EURUSD"] == shipped["provenance"]["EURUSD"]


def test_no_mt5_and_no_search_dependencies_are_imported():
    import demo.opportunity.engine
    import demo.opportunity.replay  # noqa: F401

    for banned in ("MetaTrader5", "optuna", "deap"):
        assert banned not in sys.modules, banned


def test_load_default_twice_is_identical():
    assert load_production_spec().strategy_hash == load_production_spec().strategy_hash
