import pytest

from research.walk_forward import WalkForwardConfig, make_walk_forward_windows


def test_walk_forward_keeps_training_and_oos_observations_separate() -> None:
    windows = make_walk_forward_windows(
        tuple(range(10)),
        WalkForwardConfig(in_sample_size=4, out_of_sample_size=2, step_size=2),
    )

    assert [(window.in_sample, window.out_of_sample) for window in windows] == [
        ((0, 1, 2, 3), (4, 5)),
        ((2, 3, 4, 5), (6, 7)),
        ((4, 5, 6, 7), (8, 9)),
    ]
    assert all(
        set(window.in_sample).isdisjoint(window.out_of_sample) for window in windows
    )


def test_walk_forward_rejects_an_oos_window_larger_than_available_history() -> None:
    with pytest.raises(ValueError, match="complete out-of-sample window"):
        make_walk_forward_windows(
            (0, 1, 2),
            WalkForwardConfig(in_sample_size=2, out_of_sample_size=2, step_size=1),
        )
