"""Train-only fitness for the discovery search.  Research only.

``train_fitness(view)`` is a CONSERVATIVE, cost-robust, complexity-aware quality score in R
units.  It is deliberately NOT PnL: position size / leverage never enters, total profit is not
rewarded, and a fragile high-PnL candidate scores below a clean smaller one.

The function accepts a ``TrainView`` only.  The Validation partition is not reachable from it
(the type carries no such field), so no search-time decision can depend on out-of-sample-adjacent
data.

Definition (all statistics from the TRAIN partition under COMBINED_ADVERSE costs, so cost
robustness is inside the objective)::

    n < MIN_TRAIN_TRADES (60)  ->  -10 + n/60             (graded, below every valid candidate)
    n >= MIN_TRAIN_TRADES:
      base   = E[R] - 1.0 * SE                    day-clustered SE => a ~84% lower bound
      - 0.02 * complexity                          clause/OR/stop-kind count
      - 0.5  * max(0, top3_share - 0.35)           top-3 winners' share of positive R
      - 0.15 * max(0, -min_chunk_expectancy)       any of 3 equal-calendar Train chunks < 0
      - 0.01 * max(0, max_loss_streak - 8)
      - 0.05 * max(0, max_dd_r / sqrt(n) - 3)      drawdown normalised by sqrt(n): a
                                                   zero-drift R-walk has max DD ~ 1.25*sd*sqrt(n),
                                                   sd(R) ~ 1..1.5 for fixed-R exits, so 3 is a
                                                   generous "normal" ceiling; only excess counts
      + 0.02 * min(1, n / 300)                     small sample-size bonus, saturating

Chunks without trades are ignored by the consistency term (their emptiness already shows in
trades/day, and n>=60 bounds the damage); ``None`` metrics contribute 0.
"""

from __future__ import annotations

import math

from alpha.discovery.evaluate import MIN_TRAIN_TRADES, TrainView

MIN_TRADES = MIN_TRAIN_TRADES  # backward-compatible alias
INVALID_FLOOR = -10.0  # below any attainable valid fitness
W_SE = 1.0
W_COMPLEXITY = 0.02
W_TOP3, TOP3_FREE = 0.5, 0.35
W_CHUNK = 0.15
W_STREAK, STREAK_FREE = 0.01, 8
W_DD, DD_FREE = 0.05, 3.0
W_SAMPLE, SAMPLE_FULL = 0.02, 300


def train_fitness(view: TrainView) -> float:
    """Higher is better.  See the module docstring for the exact definition."""
    side = view.adverse
    screen = side.screen
    n = screen.n_trades
    if n < MIN_TRAIN_TRADES or screen.expectancy_r is None:
        return INVALID_FLOOR + n / MIN_TRAIN_TRADES
    score = screen.expectancy_r - W_SE * (side.se_r or 0.0)
    score -= W_COMPLEXITY * view.complexity
    top3 = screen.top_3_positive_r_share
    if top3 is not None:
        score -= W_TOP3 * max(0.0, top3 - TOP3_FREE)
    chunks = [e for e, k in zip(side.chunk_expectancy, side.chunk_trades, strict=False)
              if e is not None and k > 0]
    if chunks:
        score -= W_CHUNK * max(0.0, -min(chunks))
    score -= W_STREAK * max(0, screen.max_loss_streak - STREAK_FREE)
    if screen.max_drawdown_r is not None:
        score -= W_DD * max(0.0, screen.max_drawdown_r / math.sqrt(n) - DD_FREE)
    score += W_SAMPLE * min(1.0, n / SAMPLE_FULL)
    return float(score)


__all__ = ("INVALID_FLOOR", "MIN_TRAIN_TRADES", "train_fitness")
