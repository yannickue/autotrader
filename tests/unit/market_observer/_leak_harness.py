# ruff: noqa: E501
"""Reusable look-ahead (future-leak) harness for feature groups. Test helper, no tests in this module.

A feature function ``fn(bars, i) -> value`` is causal iff its value at ``i`` is a function of bars ``0..i`` ONLY. The harness checks that
in three independent ways and reports WHICH ObserverBars field leaked:

* ``prefix``  : ``fn(bars.prefix(i + 1), i) == fn(bars, i)`` (the classic prefix-invariance test).
* one field at a time: every future value (index > i) of ONE ObserverBars array is replaced by junk (random scale, NaN, extreme) while the
  rest stays as is. EVERY array is covered: ts_ns, o, h, l, c, tick_volume, spread, atr, segment_id, local_minute, local_day. The older
  scramble test only scrambled o/h/l/c/tick_volume/spread and kept atr/ts/segment_id/local_day from the ORIGINAL bars, so a leak through
  those fields was invisible. (A future ``ts_ns`` stays strictly ascending, as ``ObserverBars.validate`` demands.)
* all fields at once.

An exception raised only under perturbation counts as a leak ("raised"), as does a different result. The result is compared through a
canonical, NaN-safe form (dataclasses, mappings, sequences, ndarrays).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from market_observer.schema import ObserverBars

ARRAY_FIELDS = ("ts_ns", "o", "h", "l", "c", "tick_volume", "spread", "atr", "segment_id", "local_minute", "local_day")
FLOAT_FIELDS = ("o", "h", "l", "c", "tick_volume", "spread", "atr")
INT_FIELDS = ("segment_id", "local_minute", "local_day")
MODES = ("scale", "nan", "extreme")


def canon(x: Any) -> Any:
    """NaN-safe, hashable-ish canonical form of a feature value."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return (type(x).__name__, tuple((f.name, canon(getattr(x, f.name))) for f in dataclasses.fields(x)))
    if isinstance(x, Mapping):
        return tuple(sorted(((str(k), canon(v)) for k, v in x.items()), key=lambda kv: kv[0]))
    if isinstance(x, np.ndarray):
        return ("nd", tuple(canon(v) for v in x.tolist()))
    if isinstance(x, (list, tuple)):
        return tuple(canon(v) for v in x)
    if isinstance(x, (set, frozenset)):
        return tuple(sorted((canon(v) for v in x), key=repr))
    if isinstance(x, (float, np.floating)):
        return "nan" if math.isnan(float(x)) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    return x


def perturb_future(bars: ObserverBars, i: int, fields: Sequence[str], rng: np.random.Generator, mode: str = "scale") -> ObserverBars:
    """Copy of ``bars`` in which every value with index > ``i`` of the listed arrays is junk. Index <= i is bit-identical."""
    n = len(bars)
    k = n - i - 1
    repl: dict[str, np.ndarray] = {}
    if k <= 0:
        return bars
    for f in fields:
        cur = np.array(getattr(bars, f))
        if f == "ts_ns":
            cur[i + 1:] = cur[i] + np.cumsum(rng.integers(1, 10**13, size=k)).astype(np.int64)  # still strictly ascending
        elif f in FLOAT_FIELDS:
            if mode == "nan":
                cur[i + 1:] = np.nan
            elif mode == "extreme":
                cur[i + 1:] = rng.choice([-1e9, 1e9, 0.0, 1e-9], size=k)
            else:
                cur[i + 1:] = cur[i + 1:] * rng.uniform(0.2, 3.0, size=k) + rng.normal(0, 1.0, size=k)
        elif f in INT_FIELDS:
            hi = 1440 if f == "local_minute" else 10**6
            cur[i + 1:] = rng.integers(0, hi, size=k) if mode != "extreme" else rng.choice([-1, 0, hi], size=k)
        else:
            raise ValueError(f"unknown ObserverBars field {f!r}")
        repl[f] = cur
    return dataclasses.replace(bars, **repl)


def _evaluate(fn: Callable[[ObserverBars, int], Any], bars: ObserverBars, i: int) -> tuple[bool, Any]:
    try:
        return True, canon(fn(bars, i))
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def leaking_fields(
    fn: Callable[[ObserverBars, int], Any], bars: ObserverBars, i: int, *, seed: int = 0, modes: Sequence[str] = MODES,
    fields: Sequence[str] = ARRAY_FIELDS, check_prefix: bool = True,
) -> dict[str, str]:
    """{leak source: reason}. Empty dict == no look-ahead found at ``i``. Sources: 'prefix', '<field>:<mode>', 'ALL:<mode>'."""
    ok0, base = _evaluate(fn, bars, i)
    if not ok0:
        raise AssertionError(f"fn itself fails on the unperturbed bars at i={i}: {base}")
    found: dict[str, str] = {}
    if check_prefix:
        ok, v = _evaluate(fn, bars.prefix(i + 1), i)
        if not ok or v != base:
            found["prefix"] = "raised" if not ok else "value differs"
    for mode in modes:
        rng = np.random.default_rng([seed, i, MODES.index(mode) if mode in MODES else 99])
        for f in fields:
            ok, v = _evaluate(fn, perturb_future(bars, i, [f], rng, mode), i)
            if not ok or v != base:
                found[f"{f}:{mode}"] = "raised" if not ok else "value differs"
        ok, v = _evaluate(fn, perturb_future(bars, i, list(fields), rng, mode), i)
        if not ok or v != base:
            found[f"ALL:{mode}"] = "raised" if not ok else "value differs"
    return found


def assert_no_future_dependence(fn: Callable[[ObserverBars, int], Any], bars: ObserverBars, indices: Sequence[int], *, seed: int = 0, **kw: Any) -> None:
    bad = {i: r for i in indices if (r := leaking_fields(fn, bars, i, seed=seed, **kw))}
    if bad:
        first = next(iter(bad))
        raise AssertionError(f"look-ahead detected at {len(bad)}/{len(indices)} decision bar(s); first i={first}: {bad[first]}")


def leaked_field_names(found: Mapping[str, str]) -> set[str]:
    """Plain field names from a ``leaking_fields`` result ('prefix' and 'ALL' dropped)."""
    return {k.split(":")[0] for k in found if k not in ("prefix",) and not k.startswith("ALL")}
