# V2 capital-growth simulator

Code: `src/alpha/growth/` (`stream`, `schedules`, `sizing`, `analytics`, `engine`), runner
`research/runners/v2_growth_sim.py`, config `research/configs/v2_growth.json`, tests
`tests/test_v2_growth_engine.py`.

## What it is, and is not

It **scales a given R-multiple stream** by a risk schedule and reports the resulting distribution of
equity paths. It **never creates edge**. A zero- or negative-expectancy stream decays and eventually
ruins whatever the risk fraction (volatility drag: g(f) = E[log(1+fR)] < 0 for all f > 0 when E[R] <= 0);
the tests assert this. 30x leverage is a permitted ceiling, not a target (research cap 10x). RiskPolicy
stays authoritative; this module is research only and is not on the live path.

**No outcome of this tool is a forecast or a guarantee. 500 -> 5,000 EUR is not promised by any row.**
Aggressive fractions that "reach" 5,000 in some paths do so with large ruin/drawdown probability; the
example below shows P(5000) rising only together with P(ruin) and P(DD >= 50%).

## Model

* Input: per-trade net R (costs already inside R), day labels, optionally stop distance (price units) and
  entry price. Loaders: csv / npz / parquet (`r_multiple|r`, `entry_day|day`, `risk_pts`, `entry_price`).
* Resampling: `iid_trade` (trades/day drawn from the empirical day-count distribution, trades iid),
  `day_block` (whole days, preserves intraday clustering and loss streaks), `stationary_block` (geometric
  blocks of days, mean length `block_len`, also preserves multi-day regimes). Daily cap =
  `max_trades_per_day`. Zero-trade days enter through `total_days`.
* Edge uncertainty (`edge_uncertainty=True`): each path shifts the stream to a true mean drawn from
  N(mean, SE), SE = max(iid, day-cluster-robust). Flat-prior normal approximation; it reflects estimation
  error, it is not a model of regime change or of selection bias (see below).
* Compounding: risk per trade = f x CURRENT equity. Schedules: fixed fractions, fractional Kelly (fraction of
  numerical f* of the ESTIMATED stream, optionally shrunk by k SE; never the hidden true mean),
  drawdown throttle (halve at -15% from peak, restore at a new high), ramp after consecutive wins (capped).
* Lot policies (need `stop_pts` and the market `LotSizing` from `market_costs`): `skip` rounds lots DOWN to the
  lot step and skips the trade if below min lot; `forced_min_lot` rounds UP to min lot and reports the
  realised risk (`mean_realised_over_target`, `max_realised_risk_frac`). Leverage cap: lots limited so
  notional/equity <= cap, otherwise skipped. `ideal` = continuous sizing (with an implied leverage cap when
  stop and price are known).
* Ruin: equity < `ruin_frac` x start (20%); the path is then frozen (absorbing).
* Outputs per horizon (60/120/250 days): ending-capital p5/25/50/75/95, P(reach 1000/2500/5000) and
  median days to target, P(maxDD >= 25%/50%), P(ruin), max-DD and longest-loss-streak percentiles, skip
  fractions, realised-risk stats; analytic g(f), Kelly f*, and `required_fraction` (risk fraction whose
  MEDIAN growth reaches the target; `None` when even f* cannot). Survival at that f = run the MC at it.

Finding worth knowing: at 500 EUR on GER40 the minimum lot (0.25) is about 12.7x equity notional, i.e.
ABOVE the 10x research cap, so with `leverage_cap=10` every lot-based trade is skipped (both policies).
Only the 30x ceiling (a permitted upper bound, not a target) lets a 500 EUR account trade GER40 at all.

## NOT modelled

Costs beyond those inside R, regime change / non-stationarity, selection bias of the stream (a stream
picked because it looked good overstates edge; edge uncertainty only covers sampling error), correlation
between markets, margin/stop-out mechanics, gaps beyond the sampled R, withdrawals/taxes, broker
rejection, changing stop distances with price level, intraday equity path within a day, execution
latency. R < -1 outcomes appear only if the stream has them; equity is clamped at 0.

## Interpretation rules

1. Read P(ruin), P(DD>=50%) and the p5 column next to every "median" or P(target).
2. A stream with mean/SE < ~2-3 has no established edge: treat all rows as an exploration of the noise.
3. Never choose f from the row that maximises P(5000); f* is an upper bound on a sane fraction.
4. Fractions above f* lower median growth AND raise ruin (test-asserted).
5. Do not quote any row as an expectation for real trading.

## Example (synthetic, 6,000 trades, win rate 40%, 1.2 trades/day, day_block, `ideal`, 500 EUR start, 250 days, 4,000 paths)

Kelly f* of the +0.15R stream = 7.9%; the 0R stream has f* = 0. Even at f*, median growth cannot reach
5,000 in 250 days (`required_fraction` = None). 0R rows: the ~97-99 values are the frozen-at-ruin equity.

| f | +0.15R median | P(5000) | P(ruin) | P(DD>=50%) | 0R median | P(5000) | P(ruin) | P(DD>=50%) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5% | 624 | 0.000 | 0.000 | 0.000 | 499 | 0.000 | 0.000 | 0.000 |
| 1% | 767 | 0.000 | 0.000 | 0.000 | 493 | 0.000 | 0.000 | 0.002 |
| 2% | 1107 | 0.001 | 0.000 | 0.019 | 466 | 0.000 | 0.000 | 0.217 |
| 3% | 1509 | 0.061 | 0.000 | 0.176 | 419 | 0.000 | 0.019 | 0.595 |
| 5% | 2364 | 0.341 | 0.015 | 0.736 | 299 | 0.006 | 0.238 | 0.952 |
| 8% | 2933 | 0.520 | 0.128 | 0.992 | 99 | 0.035 | 0.616 | 0.999 |
| 10% | 2328 | 0.533 | 0.256 | 1.000 | 97 | 0.053 | 0.766 | 1.000 |

The 0R stream reaches 5,000 in up to 5% of paths at 10% risk (pure variance) while ruining in 77%: the
"P(5000)" column alone would mislead. Full tables (incl. edge uncertainty, lot policies, dynamic
schedules): `research/reports/v2_growth/<label>/growth_summary.{json,md}`.

Lot policies, +0.15R stream with 25-point stops, GER40 at 500 EUR, 30x ceiling, 250 days: fixed 1% ->
`skip` skips 71% of trades (median 529), `forced_min_lot` trades all with realised risk about 1.09x
target on average (median 819); the realised risk of a forced min lot is 1.25% of equity at a 25-pt stop and grows
as stops widen, which is the small-account inflation this policy makes visible.
