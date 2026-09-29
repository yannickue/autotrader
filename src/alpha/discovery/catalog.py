"""Curated feature catalog for the strategy grammar (research only).

Thresholds are never stored as values: a clause carries a QUANTILE ``q`` that a
``ThresholdResolver`` resolves against TRAIN-partition values only.  All catalog entries are
written in the LONG frame; the ``mirror`` rule says how the same clause reads for SHORT:

* ``self``          identical rule (direction-agnostic feature, e.g. ADX, context flags)
* ``reflect``       same feature, op flipped, value -> 2*center - value (negate for center 0)
* ``pair``          partner feature, same op and value (brk_up<->brk_dn, from_high<->from_low)
* ``pair_reflect``  partner feature, op flipped, value reflected (dist_pdh<->dist_pdl)
* ``level``         ``close <op> level`` rules: partner level (or the same level) with op flipped

Semantic assumptions for features that a parallel worktree adds (documented, not verifiable
until they exist): dist_*_atr = (close - level)/ATR signed; brk_up_N = close above the prior
N-bar high; from_high_N_atr = (N-bar high - close)/ATR >= 0; sweep_hi_N = wick above prior
N-bar high with close back below; bar_close_loc in [0,1]; wick/body/range ratios unsigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from alpha.fast import spec as _spec

Layer = Literal["REGIME", "CONTEXT", "TRIGGER", "LEVEL", "TIME"]
Kind = Literal["continuous", "flag", "signed", "label", "fixed", "level", "time"]

CONTINUOUS_OPS = (">", "<")
_FLIP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "in": "in"}


def flip_op(op: str) -> str:
    return _FLIP[op]


@dataclass(frozen=True)
class Mirror:
    kind: Literal["self", "reflect", "pair", "pair_reflect", "level"] = "self"
    partner: str | None = None  # catalog entry name of the mirrored feature
    center: float = 0.0
    labels: tuple[tuple[str, str], ...] = ()  # label -> mirrored label (regime labels)


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    layer: Layer
    kind: Kind
    role: str
    ops: tuple[str, ...] = CONTINUOUS_OPS
    q_lo: float = 0.1
    q_hi: float = 0.9
    mirror: Mirror = field(default_factory=Mirror)
    rule_feature: str | None = None  # feature the Rule reads (defaults to name)
    other_feature: str | None = None  # for kind == "level": close <op> other_feature
    fixed_value: float | None = None  # kind == "fixed": constant threshold
    dimension: str | None = None  # kind == "label": regime dimension
    labels: tuple[str, ...] = ()
    alias_group: str | None = None

    @property
    def feature(self) -> str:
        return self.rule_feature or self.name

    def required_features(self) -> tuple[str, ...]:
        names = [self.feature]
        if self.other_feature:
            names.append(self.other_feature)
        return tuple(names)


_E: list[CatalogEntry] = []


def _add(name: str, layer: Layer, kind: Kind, role: str, **kw) -> None:
    _E.append(CatalogEntry(name, layer, kind, role, **kw))


_R = Mirror("reflect", center=0.0)
_SELF = Mirror("self")


def _pair(a: str, b: str, *, reflect: bool = False) -> tuple[Mirror, Mirror]:
    kind = "pair_reflect" if reflect else "pair"
    return Mirror(kind, partner=b), Mirror(kind, partner=a)


# ------------------------------------------------------------------ REGIME (H1)
_add("h1_adx14", "REGIME", "continuous", "H1 trend strength: trends persist, ranges mean-revert",
     q_lo=0.3, q_hi=0.9, alias_group="trend_strength")
_add("h1_efficiency_ratio", "REGIME", "continuous",
     "H1 directional efficiency separates trend from chop", q_lo=0.3, q_hi=0.9)
_add("h1_volatility_percentile", "REGIME", "continuous",
     "H1 volatility state gates breakout vs fade edges", alias_group="vol_state")
_add("h1_ema_slope", "REGIME", "signed", "H1 EMA slope is the higher-timeframe bias", mirror=_R,
     q_lo=0.2, q_hi=0.9)
_add("h1_bollinger_width", "REGIME", "continuous", "H1 band width: squeeze vs expansion regime")
_add("regime_direction", "REGIME", "label",
     "classified H1 direction (UP/DOWN/NEUTRAL) as bias filter",
     ops=("in",), dimension="DIRECTION", labels=("UP", "DOWN", "NEUTRAL"),
     mirror=Mirror("self", labels=(("UP", "DOWN"), ("DOWN", "UP"), ("NEUTRAL", "NEUTRAL"))))
_add("regime_trend_strength", "REGIME", "label",
     "classified trend strength: trending vs range-like",
     ops=("in",), dimension="TREND_STRENGTH", labels=("TRENDING", "WEAK", "RANGE_LIKE"),
     alias_group="trend_strength")
_add("regime_volatility", "REGIME", "label", "classified volatility level (LOW/NORMAL/HIGH)",
     ops=("in",), dimension="VOLATILITY", labels=("LOW", "NORMAL", "HIGH"),
     alias_group="vol_state")
_add("regime_vol_state", "REGIME", "label", "compression vs expansion volatility state",
     ops=("in",), dimension="VOL_STATE", labels=("COMPRESSION", "EXPANSION"))

# ------------------------------------------------------------------ CONTEXT (M15)
_add("m15_adx14", "CONTEXT", "continuous", "M15 trend strength at the setup timeframe",
     q_lo=0.3, q_hi=0.9)
_add("m15_efficiency_ratio", "CONTEXT", "continuous", "M15 efficiency: smooth trend vs noisy range")
_add("m15_range_position", "CONTEXT", "continuous",
     "location inside the M15 range (fade highs, buy lows)", mirror=Mirror("reflect", center=0.5))
_add("m15_volatility_percentile", "CONTEXT", "continuous", "M15 volatility state for the setup")
_add("m15_ema_slope", "CONTEXT", "signed", "M15 EMA slope: setup-timeframe momentum", mirror=_R,
     q_lo=0.2, q_hi=0.9)
_add("compression_expansion_ratio", "CONTEXT", "continuous",
     "short/long range ratio: <1 compressed, >1 expanding", alias_group="compress")
_add("range_ratio_12_48", "CONTEXT", "continuous",
     "12-bar vs 48-bar range: contraction before expansion", alias_group="compress")
_FLAGS = {
    "trend_continuation": "established trend still intact",
    "pullback": "shallow counter-move inside a trend (buy the dip)",
    "consolidation": "price coiling in a tight box",
    "compression": "volatility squeeze precedes expansion",
    "range_extreme": "price at the edge of a range (fade candidate)",
    "breakout_setup": "price parked at a breakout boundary",
    "retest": "return to a broken level",
    "failed_breakout": "breakout that trapped participants",
    "momentum_continuation": "impulse leg still extending",
    "reversal_context": "conditions favouring a turn",
}
for _n, _why in _FLAGS.items():
    _add(f"context_{_n}", "CONTEXT", "flag", f"M15 context flag: {_why}", ops=("==",),
         alias_group="compress" if _n == "compression" else None)

# ------------------------------------------------------------------ TRIGGER (M5)
_add("m5_adx14", "TRIGGER", "continuous", "M5 trend strength at entry", q_lo=0.3, q_hi=0.9)
_add("m5_rsi14", "TRIGGER", "continuous", "M5 RSI: exhaustion (mean reversion) or strength",
     mirror=Mirror("reflect", center=50.0), q_lo=0.05, q_hi=0.95)
_add("m5_volatility_percentile", "TRIGGER", "continuous",
     "M5 volatility percentile: expansion bars")
_add("m5_bollinger_width", "TRIGGER", "continuous", "M5 band width: squeeze vs expansion at entry")
_add("m5_range_position", "TRIGGER", "continuous", "close location inside the M5 range",
     mirror=Mirror("reflect", center=0.5))
_add("m5_efficiency_ratio", "TRIGGER", "continuous", "M5 efficiency: clean impulse vs chop")
_add("m5_ema_slope", "TRIGGER", "signed", "M5 EMA slope: short-term momentum sign", mirror=_R,
     q_lo=0.2, q_hi=0.9)
_add("m5_normalized_return", "TRIGGER", "signed", "last M5 return in ATR units", mirror=_R,
     q_lo=0.2, q_hi=0.9)
_add("bar_range_atr", "TRIGGER", "continuous", "entry-bar range/ATR: expansion bars",
     q_lo=0.2, q_hi=0.95)
_add("bar_body_ratio", "TRIGGER", "continuous", "body share of the bar: conviction",
     q_lo=0.2, q_hi=0.95)
_add("bar_close_loc", "TRIGGER", "continuous", "where the bar closed within its range",
     mirror=Mirror("reflect", center=0.5), q_lo=0.1, q_hi=0.95)
_a, _b = _pair("upper_wick_ratio", "lower_wick_ratio")
_add("upper_wick_ratio", "TRIGGER", "continuous", "upper wick share: rejection of higher prices",
     mirror=_a, q_lo=0.3, q_hi=0.95)
_add("lower_wick_ratio", "TRIGGER", "continuous", "lower wick share: rejection of lower prices",
     mirror=_b, q_lo=0.3, q_hi=0.95)
_add("bar_dir", "TRIGGER", "fixed", "signed bar direction (+1 up / -1 down)", mirror=_R,
     fixed_value=0.0)
for _k in (3, 6, 12):
    _add(f"mom_{_k}_atr", "TRIGGER", "signed", f"{_k}-bar momentum in ATR units", mirror=_R,
         q_lo=0.2, q_hi=0.95)
for _n in (20, 48):
    _a, _b = _pair(f"brk_up_{_n}", f"brk_dn_{_n}")
    _add(f"brk_up_{_n}", "TRIGGER", "flag", f"close breaks the prior {_n}-bar high",
         ops=("==",), mirror=_a)
    _add(f"brk_dn_{_n}", "TRIGGER", "flag", f"close breaks the prior {_n}-bar low",
         ops=("==",), mirror=_b)
_a, _b = _pair("from_high_24_atr", "from_low_24_atr")
_add("from_high_24_atr", "TRIGGER", "continuous",
     "pullback depth from the 24-bar high in ATR", mirror=_a, q_lo=0.2, q_hi=0.9)
_add("from_low_24_atr", "TRIGGER", "continuous",
     "bounce height from the 24-bar low in ATR", mirror=_b, q_lo=0.2, q_hi=0.9)
for _up, _dn, _what in (
    ("sweep_hi_20", "sweep_lo_20", "20-bar extreme"),
    ("sweep_pdh", "sweep_pdl", "previous-day extreme"),
):
    _a, _b = _pair(_up, _dn)
    _add(_up, "TRIGGER", "flag", f"liquidity sweep above the {_what} then reclaim",
         ops=("==",), mirror=_a)
    _add(_dn, "TRIGGER", "flag", f"liquidity sweep below the {_what} then reclaim",
         ops=("==",), mirror=_b)

# ------------------------------------------------------------------ LEVEL (structure distances)
for _h, _l, _lab in (
    ("dist_pdh_atr", "dist_pdl_atr", "previous-day high/low"),
    ("dist_sess_high_atr", "dist_sess_low_atr", "session high/low"),
    ("dist_swing_high_atr", "dist_swing_low_atr", "last swing high/low"),
):
    _a, _b = _pair(_h, _l, reflect=True)
    _add(_h, "LEVEL", "signed", f"signed ATR distance to {_lab} (high side)", mirror=_a,
         q_lo=0.1, q_hi=0.9)
    _add(_l, "LEVEL", "signed", f"signed ATR distance to {_lab} (low side)", mirror=_b,
         q_lo=0.1, q_hi=0.9)
_add("dist_pdc_atr", "LEVEL", "signed", "signed ATR distance to previous close", mirror=_R,
     q_lo=0.1, q_hi=0.9)
_add("dist_sess_open_atr", "LEVEL", "signed", "signed ATR distance to session open", mirror=_R,
     q_lo=0.1, q_hi=0.9)
_add("gap_atr", "LEVEL", "signed", "opening gap in ATR units", mirror=_R, q_lo=0.1, q_hi=0.9)
# close-vs-level rules that need no new features (rule other_feature)
for _h, _l, _hf, _lf, _ops, _lab in (
    ("lvl_c_pdh", "lvl_c_pdl", "previous_day_high", "previous_day_low", (">", "<"),
     "previous-day high/low"),
    ("lvl_c_sess_high", "lvl_c_sess_low", "session_high", "session_low", (">=", "<="),
     "session high/low"),
    ("lvl_c_swing_high", "lvl_c_swing_low", "last_swing_high", "last_swing_low", (">", "<"),
     "last swing"),
):
    _add(_h, "LEVEL", "level", f"close relative to the {_lab} (high side)", ops=_ops,
         rule_feature="c", other_feature=_hf, mirror=Mirror("level", partner=_l))
    _add(_l, "LEVEL", "level", f"close relative to the {_lab} (low side)", ops=_ops,
         rule_feature="c", other_feature=_lf, mirror=Mirror("level", partner=_h))
_add("lvl_c_sess_open", "LEVEL", "level", "close above/below the session open (intraday bias)",
     ops=(">", "<"), rule_feature="c", other_feature="session_open", mirror=Mirror("level"))
_add("lvl_c_pdc", "LEVEL", "level", "close above/below the previous close", ops=(">", "<"),
     rule_feature="c", other_feature="previous_day_close", mirror=Mirror("level"))

# ------------------------------------------------------------------ TIME
_add("berlin_minute", "TIME", "time", "intraday window in Berlin minutes (session phase edge)",
     ops=(), mirror=_SELF)

CATALOG: dict[str, CatalogEntry] = {e.name: e for e in _E}
assert len(CATALOG) == len(_E), "duplicate catalog names"

TIME_DOMAIN = (420, 1230)  # 07:00 .. 20:30 Berlin
TIME_GRID = 15
STOP_MULT_DOMAIN = (0.8, 3.0)
STOP_MULT_GRID = 0.1
TARGET_R_DOMAIN = (1.0, 4.0)
TARGET_R_GRID = 0.25
STOP_OFFSET_DOMAIN = (0.0, 15.0)
STOP_OFFSET_GRID = 2.5
Q_GRID = 0.05
STOP_LEVELS = ("previous_day_high", "previous_day_low", "session_high", "session_low")
STOP_LEVEL_MIRROR = {
    "previous_day_high": "previous_day_low", "previous_day_low": "previous_day_high",
    "session_high": "session_low", "session_low": "session_high",
}

ALIAS_GROUPS: dict[str, tuple[str, ...]] = {}
for _e in CATALOG.values():
    if _e.alias_group:
        ALIAS_GROUPS[_e.alias_group] = (*ALIAS_GROUPS.get(_e.alias_group, ()), _e.name)


class FeaturePool:
    """Catalog restricted to what the FeatureSet AND the spec's FEATURE_NAMES really provide."""

    def __init__(self, available: set[str] | frozenset[str]) -> None:
        avail = set(available) & set(_spec.FEATURE_NAMES)
        self.available = frozenset(avail)
        ok = {
            n for n, e in CATALOG.items()
            if e.kind != "time" and all(f in avail for f in e.required_features())
        }
        # partner-dependent mirrors need the partner present, otherwise SHORT is not expressible
        changed = True
        while changed:
            changed = False
            for n in list(ok):
                partner = CATALOG[n].mirror.partner
                if partner is not None and partner not in ok:
                    ok.discard(n)
                    changed = True
        self.names = frozenset(ok)
        self.entries = {n: CATALOG[n] for n in sorted(ok)}
        self.by_layer: dict[str, list[str]] = {}
        for n, e in self.entries.items():
            self.by_layer.setdefault(e.layer, []).append(n)

    @classmethod
    def from_features(cls, features) -> FeaturePool:
        return cls(set(features.keys()))

    def has(self, name: str) -> bool:
        return name in self.names

    def first(self, *names: str) -> str | None:
        return next((n for n in names if n in self.names), None)

    def layer(self, *layers: str) -> list[str]:
        return [n for layer in layers for n in self.by_layer.get(layer, [])]


__all__ = (
    "ALIAS_GROUPS", "CATALOG", "CatalogEntry", "FeaturePool", "Mirror", "flip_op",
)
