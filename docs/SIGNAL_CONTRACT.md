# Signal Contract

A `Signal` is a strategy opinion, not an order. Strategies have no venue adapter dependency and no
authority to size positions, select leverage, or place/cancel orders.

| Field | Contract |
|---|---|
| `signal_id` | Globally unique idempotency key |
| `instrument` | Canonical instrument identifier |
| `direction` | `long` or `short` |
| `timestamp` | UTC event time of the decision |
| `strategy_id` | Versioned strategy/configuration identity |
| `entry_zone` | Inclusive low/high Decimal price range; not an executable limit order |
| `invalidation_level` | Price/condition level that invalidates the thesis |
| `expected_move` | Decimal expected return/move using strategy-declared units |
| `expected_horizon` | Positive duration for expiry and evaluation |
| `confidence` | Calibrated Decimal in `[0, 1]`; never a leverage instruction |
| `metadata` | Versioned diagnostics, feature/data versions, and trace identifiers |

Consumers deduplicate by `signal_id`. A signal expires at the earlier of its explicit policy expiry,
expected-horizon deadline, data-staleness deadline, or invalidation event. Expired/stale signals
cannot open or increase a position. New strategy versions use new `strategy_id` values; a deployed
strategy/configuration is immutable and reproducible.

Confidence is optional evidence for selection and calibration. Leverage is derived by risk from the
risk budget, stop distance, position notional, liquidity, volatility, and portfolio constraints; it
is never selected from confidence alone.

