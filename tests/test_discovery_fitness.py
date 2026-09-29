from __future__ import annotations

import inspect
from dataclasses import replace
from itertools import pairwise

import pytest

from alpha.discovery import fitness as fitness_module
from alpha.discovery.evaluate import GenomeEval, SideMetrics, TrainView, validation_gate_view
from alpha.discovery.fitness import train_fitness
from alpha.fast.screen import PartitionScreen


def _screen(n=200, exp=0.10, top3=0.20, dd=20.0, streak=5):
    return PartitionScreen(
        n_trades=n, expectancy_r=exp, profit_factor=1.2, win_rate=0.4, avg_winner_r=1.5,
        avg_loser_r=-1.0, payoff=1.5, max_drawdown_r=dd, max_loss_streak=streak,
        trades_per_day=1.0, zero_trade_day_frac=0.3, cost_burden=0.05,
        top_3_positive_r_share=top3, mean_mfe_r=0.6, mean_mae_r=-0.5,
    )


def _side(n=200, exp=0.10, se=0.05, chunks=(0.1, 0.1, 0.1), **kw):
    return SideMetrics(_screen(n, exp, **kw), se, exp - 2 * se, exp + 2 * se, chunks,
                       (n // 3,) * 3)


def _view(n=200, exp=0.10, se=0.05, complexity=3, chunks=(0.1, 0.1, 0.1), base_exp=0.15, **kw):
    return TrainView("h", complexity, _side(n, base_exp), _side(n, exp, se, chunks, **kw))


def test_too_few_trades_is_graded_negative():
    assert train_fitness(_view(n=0)) == -10.0
    assert train_fitness(_view(n=15)) == pytest.approx(-9.5)
    assert -9.04 < train_fitness(_view(n=29)) < -9.0  # graded, far below any valid candidate
    assert train_fitness(_view(exp=0.40, se=0.05, complexity=3)) > 0
    assert train_fitness(_view(n=10)) < train_fitness(_view(n=20)) < -9


def test_more_complexity_strictly_lowers_fitness():
    vals = [train_fitness(_view(complexity=c)) for c in range(2, 9)]
    assert all(a > b for a, b in pairwise(vals))


def test_concentration_lowers_fitness_only_above_threshold():
    low = train_fitness(_view(top3=0.20))
    edge = train_fitness(_view(top3=0.35))
    high = train_fitness(_view(top3=0.70))
    assert low == edge and high < edge


def test_negative_chunk_lowers_fitness():
    ok = train_fitness(_view(chunks=(0.1, 0.1, 0.1)))
    bad = train_fitness(_view(chunks=(0.1, -0.3, 0.1)))
    worse = train_fitness(_view(chunks=(0.1, -0.6, 0.1)))
    assert ok > bad > worse


def test_cost_stress_negative_below_positive():
    assert train_fitness(_view(exp=-0.05)) < train_fitness(_view(exp=0.10))
    # fitness reads COMBINED_ADVERSE, not BASE
    strong_base = replace(_view(exp=-0.05), base=_side(exp=0.5))
    assert train_fitness(strong_base) == train_fitness(_view(exp=-0.05))


def test_standard_error_is_a_conservative_haircut():
    assert train_fitness(_view(se=0.02)) > train_fitness(_view(se=0.20))


def test_streak_and_drawdown_penalties():
    assert train_fitness(_view(streak=8)) == train_fitness(_view(streak=3))
    assert train_fitness(_view(streak=14)) < train_fitness(_view(streak=8))
    assert train_fitness(_view(dd=5)) == train_fitness(_view(dd=20))  # 20/sqrt(200) < 3
    assert train_fitness(_view(dd=120)) < train_fitness(_view(dd=20))


def test_sample_size_bonus_saturates():
    assert train_fitness(_view(n=300)) == pytest.approx(train_fitness(_view(n=900)))
    assert train_fitness(_view(n=300)) > train_fitness(_view(n=60))


def test_fitness_not_monotone_in_total_pnl():
    # A: more total R (n * expectancy) but fragile: complex, concentrated, a losing chunk
    a = _view(n=400, exp=0.16, se=0.05, complexity=7, chunks=(0.6, 0.5, -0.5), top3=0.55)
    # B: less total R, clean
    b = _view(n=150, exp=0.12, se=0.05, complexity=3)
    assert 400 * 0.16 > 150 * 0.12
    assert train_fitness(a) < train_fitness(b)


def test_fitness_takes_only_a_train_view():
    sig = inspect.signature(train_fitness)
    assert list(sig.parameters) == ["view"]
    assert sig.parameters["view"].annotation in (TrainView, "TrainView")
    src = inspect.getsource(fitness_module)
    for token in ("ValidationView", "validation_gate_view", "_validation", ".validation"):
        assert token not in src
    assert not any("valid" in f for f in TrainView.__dataclass_fields__)


def test_fitness_independent_of_validation_fields():
    train = _view()
    ev_a = GenomeEval("h", "L", 3, 100, None, train, _validation=_val(0.3))
    ev_b = GenomeEval("h", "L", 3, 100, None, train, _validation=_val(-0.9))
    assert validation_gate_view(ev_a) != validation_gate_view(ev_b)
    assert train_fitness(ev_a.train) == train_fitness(ev_b.train)


def _val(exp):
    from alpha.discovery.evaluate import ValidationView

    return ValidationView(_side(60, exp), _side(60, exp))
