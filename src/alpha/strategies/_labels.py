"""Memoised causal label/view derivation shared by every strategy built on the same frame.

Regime, context and MTF derivation depend only on the input frame, not on strategy parameters,
so all variants evaluated on one frame reuse them. Entries are keyed by frame identity AND a
content hash (a mutated frame never hits a stale entry) and are returned as copies so a strategy
that edits its labels in place (tests force labels) cannot contaminate another strategy.
"""

from __future__ import annotations

import pandas as pd

from alpha.context import CONTEXT_LABELS, classify_context
from alpha.regime import REGIME_DIMENSIONS, classify_regime
from alpha.timeframe import MtfView

_CACHE: dict[tuple[int, int, int], tuple[pd.DataFrame, MtfView, pd.DataFrame, pd.DataFrame]] = {}
_MAX_ENTRIES = 4


def _key(frame: pd.DataFrame) -> tuple[int, int, int]:
    content = int(pd.util.hash_pandas_object(frame, index=True).sum() % (2**62))
    return id(frame), len(frame), content


class LabelLookup:
    """Positional snapshot of regime/context labels for ONE causal run (runtime-only).

    Values are converted exactly as the label-based lookups convert them (``str`` for regime
    dimensions, ``bool`` for context labels); it must be rebuilt whenever the label frames may
    have changed, which is why strategies build it in ``begin_run`` and drop it in ``end_run``.
    """

    def __init__(self, regime: pd.DataFrame, context: pd.DataFrame) -> None:
        self._regime_pos = stamp_positions(regime.index)
        self._context_pos = stamp_positions(context.index)
        regime_columns = [
            [str(v) for v in regime[d].to_numpy(dtype=object)] for d in REGIME_DIMENSIONS
        ]
        context_columns = [
            [bool(v) for v in context[c].to_numpy(dtype=object)] for c in CONTEXT_LABELS
        ]
        self._regime_rows = list(zip(*regime_columns, strict=True))
        self._context_rows = list(zip(*context_columns, strict=True))

    def regime(self, stamp: pd.Timestamp) -> dict[str, str]:
        row = self._regime_rows[self._regime_pos[stamp.value]]
        return dict(zip(REGIME_DIMENSIONS, row, strict=True))

    def context(self, stamp: pd.Timestamp) -> dict[str, bool]:
        row = self._context_rows[self._context_pos[stamp.value]]
        return dict(zip(CONTEXT_LABELS, row, strict=True))


def stamp_positions(index: pd.Index) -> dict[int, int]:
    """Map each timestamp (UTC nanoseconds) of a unique index to its integer position."""
    stamps = pd.DatetimeIndex(index)
    if stamps.has_duplicates:
        raise ValueError("label index must be unique")
    return {value: position for position, value in enumerate(stamps.as_unit("ns").asi8.tolist())}


def frame_labels(frame: pd.DataFrame) -> tuple[MtfView, pd.DataFrame, pd.DataFrame]:
    """Return (view, regime, context) for ``frame``; regime/context are private copies."""

    key = _key(frame)
    hit = _CACHE.get(key)
    if hit is None:
        if len(_CACHE) >= _MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        # the frame reference is kept so its id cannot be recycled while the entry lives
        hit = (frame, MtfView(frame), classify_regime(frame), classify_context(frame))
        _CACHE[key] = hit
    _, view, regime, context = hit
    return view, regime.copy(), context.copy()
