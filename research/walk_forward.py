"""Time-ordered walk-forward window construction."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkForwardConfig:
    in_sample_size: int
    out_of_sample_size: int
    step_size: int
    anchored: bool = False

    def __post_init__(self) -> None:
        if min(self.in_sample_size, self.out_of_sample_size, self.step_size) <= 0:
            raise ValueError("walk-forward sizes must be positive")


@dataclass(frozen=True, slots=True)
class WalkForwardWindow[T]:
    index: int
    in_sample: tuple[T, ...]
    out_of_sample: tuple[T, ...]


def make_walk_forward_windows[T](
    observations: tuple[T, ...], config: WalkForwardConfig
) -> tuple[WalkForwardWindow[T], ...]:
    first_oos_end = config.in_sample_size + config.out_of_sample_size
    if len(observations) < first_oos_end:
        raise ValueError("not enough observations for a complete out-of-sample window")

    windows: list[WalkForwardWindow[T]] = []
    train_end = config.in_sample_size
    index = 0
    while train_end + config.out_of_sample_size <= len(observations):
        train_start = 0 if config.anchored else train_end - config.in_sample_size
        test_end = train_end + config.out_of_sample_size
        windows.append(
            WalkForwardWindow(
                index=index,
                in_sample=observations[train_start:train_end],
                out_of_sample=observations[train_end:test_end],
            )
        )
        train_end += config.step_size
        index += 1
    return tuple(windows)
