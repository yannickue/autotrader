import pytest

from research.preparation import OptimizationPlan, prepare_vectorbt_inputs


def test_optimization_plan_records_seed_space_and_oos_objective() -> None:
    plan = OptimizationPlan(
        seed=17,
        search_space={"lookback": (5, 20), "threshold": (0.1, 0.5)},
        objective="oos_expectancy",
    )

    assert plan.to_dict() == {
        "objective": "oos_expectancy",
        "search_space": {"lookback": [5, 20], "threshold": [0.1, 0.5]},
        "seed": 17,
    }


def test_vectorbt_preparation_is_dependency_free_and_column_oriented() -> None:
    prepared = prepare_vectorbt_inputs(
        (
            {"timestamp": "2026-01-01T00:00:00+00:00", "close": 100, "entry": False},
            {"timestamp": "2026-01-01T00:01:00+00:00", "close": 101, "entry": True},
        )
    )

    assert prepared == {
        "timestamp": ("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00"),
        "close": (100, 101),
        "entry": (False, True),
    }


def test_optimization_plan_defensively_freezes_the_search_space() -> None:
    source = {"lookback": (5, 20)}
    plan = OptimizationPlan(seed=17, search_space=source, objective="oos_expectancy")

    source["lookback"] = (1, 2)

    assert plan.to_dict()["search_space"] == {"lookback": [5, 20]}
    with pytest.raises(TypeError):
        plan.search_space["lookback"] = (1, 2)
