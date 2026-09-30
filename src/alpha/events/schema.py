# ruff: noqa: E501
"""Event registry for the V2 temporal engine (pure data, no kernels).

Frozen interface (Phase 0). Kernels, stores and the temporal spec resolve names,
array names, mirrors and availability rules ONLY through this module.
See docs/V2_TEMPORAL_ENGINE.md section 1.

Conventions fixed here (design gaps resolved, see report):
* Array names are lower case: ``ev_{tf}_{name}[_{variant}]`` (tf and name lower case).
* A variant is a canonical suffix built from up to three option axes in the order
  ``src``, ``tol`` (``t0``/``t10``/``t25``), ``k`` (``k3``/``k6``). An event with no axes
  takes the empty variant; an event with axes requires an exact, explicit variant.
* Level-relative events (TOUCH, BREAK, RECLAIM, RETEST_HOLD, HOLD_ABOVE/BELOW) are
  ``bound_only``: they are evaluated by the temporal kernel against a register level and have
  no precomputed arrays.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import asdict, dataclass

EVENT_SET_VERSION = "events-v2.2"  # v2.2: ZONE_ENTER/EXIT carry the TESTED zone (evl/evx/evz)

TIMEFRAMES: tuple[str, ...] = ("M5", "M15", "H1", "D1")
TOL_GRID: tuple[float, ...] = (0.0, 0.1, 0.25)
K_GRID: tuple[int, ...] = (3, 6)

KINDS = ("pulse", "state", "level")
ROLES = ("ANCHOR", "SETUP", "TRIGGER", "CONFIRM", "CONTEXT")
LAG_RULES = ("close", "pivot_order", "second_pivot", "pattern_confirm", "state_close")

# array prefix -> dtype
PREFIX_DTYPE: dict[str, str] = {
    "ev": "uint8",
    "evl": "float64",
    "evx": "float64",
    "evo": "int32",
    "evz": "int32",
    "st": "int8",
    "lv": "float64",
    "zid": "int32",
    "sf": "float64",
    "valid": "bool",
}
MAIN_PREFIX = {"pulse": "ev", "state": "st", "level": "lv"}

ZONE_KINDS = ("swing_cluster", "prior_range", "m15_range")
SWEEP_LOW_SRCS = ("prior20", "prior48", "pdl", "session_low", "swing_low")
SWEEP_HIGH_SRCS = ("prior20", "prior48", "pdh", "session_high", "swing_high")
PATTERN_SRCS = ("double_bottom", "double_top", "inside_bar_break_up", "inside_bar_break_dn")

# Mirror of variant ``src`` values (LONG <-> SHORT frame).
SRC_MIRROR: dict[str, str] = {
    "prior20": "prior20",
    "prior48": "prior48",
    "pdl": "pdh",
    "pdh": "pdl",
    "session_low": "session_high",
    "session_high": "session_low",
    "swing_low": "swing_high",
    "swing_high": "swing_low",
    "double_bottom": "double_top",
    "double_top": "double_bottom",
    "inside_bar_break_up": "inside_bar_break_dn",
    "inside_bar_break_dn": "inside_bar_break_up",
    **{z: z for z in ZONE_KINDS},
}


def tol_code(tol: float) -> str:
    return f"t{round(tol * 100)}"


@dataclass(frozen=True)
class EventDef:
    name: str
    kind: str  # pulse | state | level
    tfs: tuple[str, ...]
    roles: tuple[str, ...]
    mirror: str | None  # partner name, own name (self-mirror) or None (no mirror)
    lag_rule: str  # availability rule (see LAG_RULES)
    lag_bars: int | None  # confirmation lag in bars; None = swing order (parameter dependent)
    causality: str
    companions: tuple[str, ...] = ()  # extra prefixes: evl, evx, evo, zid
    produces: tuple[str, ...] = ()  # capture sources usable via Capture(of=name)
    exposes: tuple[str, ...] = ()  # level events readable via Capture(source="lv")
    srcs: tuple[str, ...] = ()
    tols: tuple[float, ...] = ()
    ks: tuple[int, ...] = ()
    bound_only: bool = False  # evaluated against a register level, no arrays
    requires: tuple[str, ...] = ()  # register types a bound use needs

    @property
    def main_prefix(self) -> str:
        return MAIN_PREFIX[self.kind]

    @property
    def has_arrays(self) -> bool:
        return not self.bound_only

    def variants(self) -> tuple[str, ...]:
        """All valid variant suffixes ('' when the event has no option axes)."""
        return tuple(_variant_table(self))

    def parse_variant(self, variant: str) -> dict[str, object]:
        table = _variant_table(self)
        if variant not in table:
            raise ValueError(f"invalid variant {variant!r} for {self.name}")
        return dict(table[variant])


def _variant_table(d: EventDef) -> dict[str, dict[str, object]]:
    axes: list[tuple[str, tuple]] = []
    if d.srcs:
        axes.append(("src", d.srcs))
    if d.tols:
        axes.append(("tol", d.tols))
    if d.ks:
        axes.append(("k", d.ks))
    if not axes:
        return {"": {}}
    out: dict[str, dict[str, object]] = {}
    for combo in itertools.product(*(vals for _, vals in axes)):
        parts: dict[str, object] = {}
        toks: list[str] = []
        for (axis, _), v in zip(axes, combo, strict=True):
            parts[axis] = v
            toks.append(v if axis == "src" else tol_code(v) if axis == "tol" else f"k{v}")
        out["_".join(toks)] = parts
    return out


def _e(name, kind, tfs, roles, mirror, lag_rule, lag_bars, causality, **kw) -> EventDef:
    return EventDef(name, kind, tuple(tfs), tuple(roles), mirror, lag_rule, lag_bars, causality, **kw)


_LTF = ("M5", "M15", "H1")
_ALL = TIMEFRAMES
_P = "price"

_DEFS: list[EventDef] = [
    # ---- level-relative, kernel-evaluated against a register level -------------------------
    _e("TOUCH", "pulse", _LTF, ("SETUP", "TRIGGER", "CONFIRM"), "TOUCH", "close", 0,
       "bar range reaches level within tol_atr; known at bar close", bound_only=True, requires=(_P,)),
    _e("BREAK_UP", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "BREAK_DN", "close", 0,
       "first close above level; known at bar close", bound_only=True, requires=(_P,)),
    _e("BREAK_DN", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "BREAK_UP", "close", 0,
       "first close below level; known at bar close", bound_only=True, requires=(_P,)),
    _e("RECLAIM_UP", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "RECLAIM_DN", "close", 0,
       "close above level after a close below within k bars; known at bar close",
       bound_only=True, requires=(_P,), ks=K_GRID),
    _e("RECLAIM_DN", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "RECLAIM_UP", "close", 0,
       "close below level after a close above within k bars; known at bar close",
       bound_only=True, requires=(_P,), ks=K_GRID),
    _e("RETEST_HOLD_UP", "pulse", _LTF, ("CONFIRM", "TRIGGER"), "RETEST_HOLD_DN", "close", 0,
       "retest of level from above holds (close back above); known at bar close",
       bound_only=True, requires=(_P,)),
    _e("RETEST_HOLD_DN", "pulse", _LTF, ("CONFIRM", "TRIGGER"), "RETEST_HOLD_UP", "close", 0,
       "retest of level from below holds (close back below); known at bar close",
       bound_only=True, requires=(_P,)),
    _e("HOLD_ABOVE", "state", _LTF, ("CONFIRM", "CONTEXT"), "HOLD_BELOW", "state_close", 0,
       "closes stay above level for k bars; state at bar close",
       bound_only=True, requires=(_P,), ks=K_GRID),
    _e("HOLD_BELOW", "state", _LTF, ("CONFIRM", "CONTEXT"), "HOLD_ABOVE", "state_close", 0,
       "closes stay below level for k bars; state at bar close",
       bound_only=True, requires=(_P,), ks=K_GRID),
    # ---- pulses with arrays -----------------------------------------------------------------
    _e("SWEEP_LOW", "pulse", _LTF, ("SETUP", "TRIGGER"), "SWEEP_HIGH", "close", 0,
       "wick beyond a level known before the bar, close back inside; stamped at that close",
       companions=("evl", "evx", "evo"), produces=("evl", "evx"), srcs=SWEEP_LOW_SRCS),
    _e("SWEEP_HIGH", "pulse", _LTF, ("SETUP", "TRIGGER"), "SWEEP_LOW", "close", 0,
       "wick beyond a level known before the bar, close back inside; stamped at that close",
       companions=("evl", "evx", "evo"), produces=("evl", "evx"), srcs=SWEEP_HIGH_SRCS),
    _e("SWING_LOW_CONF", "pulse", _ALL, ("SETUP", "CONFIRM"), "SWING_HIGH_CONF", "pivot_order", None,
       "pivot at p exposed at p+order; evo holds p and is informational only",
       companions=("evl", "evo"), produces=("evl",)),
    _e("SWING_HIGH_CONF", "pulse", _ALL, ("SETUP", "CONFIRM"), "SWING_LOW_CONF", "pivot_order", None,
       "pivot at p exposed at p+order; evo holds p and is informational only",
       companions=("evl", "evo"), produces=("evl",)),
    _e("BOS_UP", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "BOS_DN", "close", 0,
       "first CLOSE above the last confirmed swing high since it was confirmed; one pulse per swing",
       companions=("evl", "evo"), produces=("evl",)),
    _e("BOS_DN", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "BOS_UP", "close", 0,
       "first CLOSE below the last confirmed swing low since it was confirmed; one pulse per swing",
       companions=("evl", "evo"), produces=("evl",)),
    _e("CHOCH_UP", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "CHOCH_DN", "close", 0,
       "BOS_UP against a prevailing down structure_state; known at bar close",
       companions=("evl", "evo"), produces=("evl",)),
    _e("CHOCH_DN", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "CHOCH_UP", "close", 0,
       "BOS_DN against a prevailing up structure_state; known at bar close",
       companions=("evl", "evo"), produces=("evl",)),
    _e("ZONE_ENTER", "pulse", _LTF, ("ANCHOR", "SETUP"), "ZONE_ENTER", "close", 0,
       "first close inside a zone known before the bar; evl/evx/evz = lo/hi/zid of THAT tested zone at the pulse bar",
       companions=("evl", "evx", "evz", "evo"), exposes=("ZONE_LO", "ZONE_HI"), srcs=ZONE_KINDS),
    _e("ZONE_EXIT", "pulse", _LTF, ("SETUP", "CONFIRM"), "ZONE_EXIT", "close", 0,
       "first close outside a zone previously entered; evl/evx/evz = lo/hi/zid of THAT tested zone at the pulse bar",
       companions=("evl", "evx", "evz", "evo"), exposes=("ZONE_LO", "ZONE_HI"), srcs=ZONE_KINDS),
    _e("TRENDLINE_TOUCH", "pulse", _LTF, ("SETUP", "TRIGGER"), "TRENDLINE_TOUCH", "second_pivot", None,
       "line through the last 2 confirmed pivots, live from the 2nd confirmation; touch known at close",
       companions=("evl",), produces=("evl",), exposes=("TRENDLINE_VALUE",), tols=TOL_GRID),
    _e("TRENDLINE_BREAK", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "TRENDLINE_BREAK", "second_pivot", None,
       "close through the line; line live from the 2nd pivot confirmation",
       companions=("evl",), produces=("evl",), exposes=("TRENDLINE_VALUE",), tols=TOL_GRID),
    _e("PATTERN_COMPLETE", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "PATTERN_COMPLETE", "pattern_confirm",
       None, "stamped at the confirming close or last confirming pivot; evo is the pattern start",
       companions=("evl", "evx", "evo"), produces=("evl", "evx"), srcs=PATTERN_SRCS),
    _e("MOMENTUM_RESUME_UP", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "MOMENTUM_RESUME_DN", "close", 0,
       "close resumes up after a shallow pullback; known at bar close",
       companions=("evl",), produces=("evl",)),
    _e("MOMENTUM_RESUME_DN", "pulse", _LTF, ("TRIGGER", "CONFIRM"), "MOMENTUM_RESUME_UP", "close", 0,
       "close resumes down after a shallow pullback; known at bar close",
       companions=("evl",), produces=("evl",)),
    # ---- states -----------------------------------------------------------------------------
    _e("TREND_UP", "state", ("M15", "H1", "D1"), ("ANCHOR", "CONTEXT"), "TREND_DN", "state_close", 0,
       "step function on complete bars only; mapped to M5 via the HTF alignment"),
    _e("TREND_DN", "state", ("M15", "H1", "D1"), ("ANCHOR", "CONTEXT"), "TREND_UP", "state_close", 0,
       "step function on complete bars only; mapped to M5 via the HTF alignment"),
    # ---- levels (lv_) -----------------------------------------------------------------------
    _e("ZONE_LO", "level", _LTF, ("CONTEXT",), "ZONE_HI", "close", 0,
       "lower edge of the current zone, known at zone creation; never rewritten",
       companions=("zid",), srcs=ZONE_KINDS),
    _e("ZONE_HI", "level", _LTF, ("CONTEXT",), "ZONE_LO", "close", 0,
       "upper edge of the current zone, known at zone creation; never rewritten",
       companions=("zid",), srcs=ZONE_KINDS),
    _e("SWING_LOW_LVL", "level", _ALL, ("CONTEXT",), "SWING_HIGH_LVL", "pivot_order", None,
       "last confirmed swing low, exposed at p+order"),
    _e("SWING_HIGH_LVL", "level", _ALL, ("CONTEXT",), "SWING_LOW_LVL", "pivot_order", None,
       "last confirmed swing high, exposed at p+order"),
    _e("TRENDLINE_VALUE", "level", _LTF, ("CONTEXT",), "TRENDLINE_VALUE", "second_pivot", None,
       "line value at this bar from the last 2 confirmed pivots",
       companions=("zid",), tols=()),
]

_REGISTRY: dict[str, EventDef] = {d.name: d for d in _DEFS}

# Features usable in feature clauses. mirror: "self" (positive-only: SHORT keeps the same test) or "neg" (signed/antisymmetric: SHORT = cmp flipped AND threshold negated, Clause.neg; exact price mirror)
FEATURE_MIRROR: dict[str, str] = {
    "atr_pct": "self",
    "range_ratio": "self",
    "ret_12": "neg",
    "dist_vwap_atr": "neg",
    "slope_20": "neg",
}

# Structure levels a next_structure target may name: name -> (tf, mirror name)
TARGET_LEVELS: dict[str, tuple[str, str]] = {
    "m5_swing_high": ("M5", "m5_swing_low"),
    "m5_swing_low": ("M5", "m5_swing_high"),
    "m15_swing_high": ("M15", "m15_swing_low"),
    "m15_swing_low": ("M15", "m15_swing_high"),
    "h1_swing_high": ("H1", "h1_swing_low"),
    "h1_swing_low": ("H1", "h1_swing_high"),
    "session_high": ("M5", "session_low"),
    "session_low": ("M5", "session_high"),
    "pdh": ("D1", "pdl"),
    "pdl": ("D1", "pdh"),
}

# ------------------------------------------------------------------------------------------
# lookup helpers
# ------------------------------------------------------------------------------------------


def get(event_name: str) -> EventDef:
    try:
        return _REGISTRY[event_name]
    except KeyError:
        raise KeyError(f"unknown event {event_name!r}") from None


def all_events() -> tuple[EventDef, ...]:
    return tuple(_REGISTRY[k] for k in sorted(_REGISTRY))


def mirror_event(name: str) -> str:
    m = get(name).mirror
    if m is None:
        raise ValueError(f"event {name} has no mirror")
    return m


def mirror_variant(name: str, variant: str) -> str:
    """Variant of ``mirror_event(name)`` matching ``variant`` of ``name`` (src mirrored)."""
    d = get(name)
    parts = d.parse_variant(variant)
    m = get(mirror_event(name))
    toks: list[str] = []
    if "src" in parts:
        toks.append(SRC_MIRROR[str(parts["src"])])
    if "tol" in parts:
        toks.append(tol_code(float(parts["tol"])))  # type: ignore[arg-type]
    if "k" in parts:
        toks.append(f"k{parts['k']}")
    out = "_".join(toks)
    if out not in m.variants():
        raise ValueError(f"mirror variant {out!r} invalid for {m.name}")
    return out


def _suffix(variant: str) -> str:
    return f"_{variant}" if variant else ""


def array_names(event: str, tf: str, variant: str = "") -> tuple[str, ...]:
    """Main array first, then companions in declaration order. Raises ValueError on misuse."""
    d = get(event)
    if not d.has_arrays:
        raise ValueError(f"{event} is bound_only and has no precomputed arrays")
    if tf not in d.tfs:
        raise ValueError(f"{event} not available on tf {tf}")
    d.parse_variant(variant)
    stem = f"{tf.lower()}_{event.lower()}{_suffix(variant)}"
    return (f"{d.main_prefix}_{stem}", *(f"{p}_{stem}" for p in d.companions))


def array_dtypes(event: str, tf: str, variant: str = "") -> dict[str, str]:
    d = get(event)
    names = array_names(event, tf, variant)
    prefixes = (d.main_prefix, *d.companions)
    return {n: PREFIX_DTYPE[p] for n, p in zip(names, prefixes, strict=True)}


def valid_array_name(tf: str) -> str:
    _check_tf(tf)
    return f"valid_{tf.lower()}"


def sf_array_names(tf: str) -> tuple[str, ...]:
    """Explicit "so far" arrays (the only place partial HTF data may appear)."""
    _check_tf(tf)
    t = tf.lower()
    return (f"sf_{t}_bar_progress", f"sf_{t}_partial_h", f"sf_{t}_partial_l")


SF_RUN_BARS = "sf_run_bars"


def _check_tf(tf: str) -> None:
    if tf not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {tf!r}")


def all_array_names() -> list[str]:
    """Every array name in the registry (all events x tfs x variants), for uniqueness checks."""
    out: list[str] = []
    for d in all_events():
        if not d.has_arrays:
            continue
        for tf in d.tfs:
            for v in d.variants():
                out.extend(array_names(d.name, tf, v))
    for tf in TIMEFRAMES:
        out.append(valid_array_name(tf))
        out.extend(sf_array_names(tf))
    out.append(SF_RUN_BARS)
    return out


def registry_fingerprint() -> str:
    """Hash of everything that defines event semantics/naming; changes with any variant edit."""
    payload = {
        "version": EVENT_SET_VERSION,
        "timeframes": TIMEFRAMES,
        "defs": [asdict(d) for d in all_events()],
        "features": FEATURE_MIRROR,
        "target_levels": TARGET_LEVELS,
        "src_mirror": SRC_MIRROR,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(b"events-registry-v1:" + blob.encode()).hexdigest()


def feature_mirror(name: str) -> str:
    try:
        return FEATURE_MIRROR[name]
    except KeyError:
        raise KeyError(f"unknown feature {name!r}") from None


def target_level(name: str) -> tuple[str, str]:
    try:
        return TARGET_LEVELS[name]
    except KeyError:
        raise KeyError(f"unknown target level {name!r}") from None


