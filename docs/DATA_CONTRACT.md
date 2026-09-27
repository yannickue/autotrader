# Data Contract

## MarketSnapshot

`MarketSnapshot` is the normalized, point-in-time input to deterministic feature and strategy code.
Required fields are:

| Field | Meaning |
|---|---|
| `instrument` | Canonical, venue-independent instrument identifier |
| `timestamp` | UTC event timestamp; receipt timestamp is added by the adapter event envelope |
| `bid`, `ask`, `last` | Decimal prices in the normalized quote convention |
| `volume` | Non-negative interval or cumulative volume with semantics declared by the adapter |
| `volatility` | Optional non-negative estimator with window/method recorded in metadata |
| `liquidity` | Optional non-negative normalized liquidity measure with method in metadata |
| `source` | Venue/feed identifier |
| `quality` | `live`, `delayed`, `stale`, or `invalid` |
| `metadata` | Versioned adapter-specific attributes; never required by generic risk logic |

The type rejects a crossed top of book (`bid > ask`). Adapters additionally validate non-empty
identifiers, finite non-negative numeric values, UTC timestamps, monotonically handled sequence
numbers, and schema version before publication.

## Quality and staleness

Staleness is determined from both event time and local receipt time using an instrument/feed policy.
Clock regressions, sequence gaps, malformed payloads, and disconnected feeds downgrade quality.
`stale` or `invalid` data cannot create new exposure. A stale-to-live transition requires a fresh
snapshot or gap recovery, not merely a reconnected socket.

## Ordering and idempotency

Every raw venue event is assigned a stable source key `(venue, channel, instrument, sequence-or-id)`.
Duplicate keys are idempotent. Gaps are recorded and either repaired from a snapshot or cause a
fail-closed state. Event time, receipt time, and persisted time must remain distinct.

## Historical storage and replay

Raw immutable events and normalized data use versioned Parquet schemas partitioned by venue,
instrument, event type, and date. Corrections create a new dataset version; they never rewrite the
provenance trail silently. Replay preserves event ordering and simulated receive timing and records
the exact dataset/version/configuration hashes.

Research datasets use point-in-time universe membership and delisting history. Future values,
back-filled indicators unavailable at the event time, and post-selection of surviving instruments
are prohibited.

