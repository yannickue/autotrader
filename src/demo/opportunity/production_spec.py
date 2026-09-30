# ruff: noqa: E501
"""FROZEN production spec of the opportunity engine (no search, no optimisation, no LLM).

What is in the file
-------------------
For each of GER40 / NAS100 / SPX500 / XAUUSD / EURUSD an explicit, ordered list of
``(family, FamilySpec parameters, fitted thresholds)`` plus provenance, sealed by ``strategy_hash``
(sha256 over the canonical JSON of everything else). The engine refuses a file whose hash does not
match, and every snapshot carries the hash in ``versions['strategy_hash']``.

Selection rule (FIXED, NOT performance driven)
----------------------------------------------
No backtest metric, ranking, grid search or trial ledger was consulted. For each family the two
"structural default" specs are used:

1. the family's *class default* spec (``ORBSpec()``, ``GAPSpec()`` ... i.e. the parameter values the
   family author declared as the central setting; LEADLAG: one spec per leader of ``leadlag.PAIRS``);
2. its *mode complement* (the same parameters with the opposite branch of the hypothesis:
   ORB breakout<->fade, GAP fade<->go, OVERNIGHT continue<->reverse, VOLREV fade<->expand,
   ROUND reject<->break(hold=3), EOD continue<->reverse), so both sides of every hypothesis produce
   discovery data. LEADLAG has no complement (sign=+1 only).

Thresholds (Train-fitted quantiles) are fitted ONCE by the family's own ``fit`` function on the
development frames restricted to Berlin dates ``<= fit_end`` (default 2026-06-30); nothing at or after
2026-09-01 is ever loaded (``load_dev_market_frame`` + ``dev_frame`` refuse it). This is a discovery
configuration: it is meant to generate opportunity flow, it does NOT claim an edge.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from alpha.common.market_data import (
    DEV_END,
    FORWARD_HOLDOUT_START,
    dev_frame,
    load_dev_market_frame,
)
from alpha.families import leadlag
from alpha.families.common import Thr
from alpha.families.data import build_family_data, build_leader_features
from alpha.families.registry import (
    FAMILY_NAMES,
    SPEC_CLASSES,
    fit_thresholds,
)
from alpha.families.registry import spec_from_dict as family_spec_from_dict
from alpha.families.spec import FamilySpec, MarketCalendar
from markets.spec import CANONICALS, PHASE2_CANONICALS, MarketSpec, load_market_spec

SCHEMA = "demo-production-spec-1"
DEFAULT_FIT_END = "2026-06-30"
DEFAULT_PATH = Path(__file__).with_name("production_spec_v1.json")
# v1.1 (Lane M2): a STRICT SUPERSET of v1 (the five v1 markets are copied verbatim) that adds the Phase-2 markets.
# v1 (file, schema, hash, behaviour) is untouched and stays the default; v1.1 is selected only when a Phase-2
# market is enabled (``load_production_spec_for``).
SCHEMA_V1_1 = "demo-production-spec-1.1"
DEFAULT_PATH_V1_1 = Path(__file__).with_name("production_spec_v1_1.json")
PHASE2_SELECTION_RULE = (
    "Phase-2 markets: ONLY the fit-free families the mechanism supports without history (ORB breakout + its fade "
    "complement, class-default parameters, thresholds = empty). GAP/OVERNIGHT/VOLREV/EOD need Train-fitted quantiles "
    "(no Phase-2 history exists at or before fit_end), ROUND needs an unresearched round-number scale and LEADLAG has no "
    "leader pairs for energy/crypto: all documented gaps, nothing invented or tuned."
)
SELECTION_RULE = (
    "per family: class-default spec + mode complement (LEADLAG: one default spec per leader); "
    "thresholds fitted once on dev frames with Berlin date <= fit_end; no performance-based selection"
)


class ProductionSpecError(ValueError):
    """The production spec file is malformed, tampered with or violates the holdout rule."""


def _enc(v: float) -> float | str:
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return float(v)


def _dec(v: float | str) -> float:
    return float(v)  # float("inf") / float("-inf") parse


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, allow_nan=False, separators=(",", ":"))


def strategy_id_of(spec: FamilySpec) -> str:
    return f"{spec.FAMILY}-{spec.canonical_hash()[:10]}"


@dataclass(frozen=True, slots=True)
class FrozenSpec:
    market: str
    spec: FamilySpec
    thr_values: tuple[float, ...]

    @property
    def family(self) -> str:
        return self.spec.FAMILY

    @property
    def strategy_id(self) -> str:
        return strategy_id_of(self.spec)

    @property
    def spec_hash(self) -> str:
        return self.spec.canonical_hash()

    @property
    def thr(self) -> Thr:
        return Thr(self.thr_values)


@dataclass(frozen=True, slots=True)
class ProductionSpecSet:
    """Immutable, hash-sealed set of frozen specs per market (ordered = evaluation order)."""

    fit_end: str
    markets: tuple[tuple[str, tuple[FrozenSpec, ...]], ...]
    provenance: str  # canonical JSON of the fit provenance (bars used per market)
    strategy_hash: str

    def specs_for(self, market: str) -> tuple[FrozenSpec, ...]:
        for m, specs in self.markets:
            if m == market:
                return specs
        raise KeyError(market)

    def market_names(self) -> tuple[str, ...]:
        return tuple(m for m, _ in self.markets)


# ------------------------------------------------------------------------------ selection
def select_specs(market: str) -> list[FamilySpec]:
    """The fixed selection rule (see module docstring)."""
    from alpha.families import eod, gap, orb, overnight, roundnum, volrev

    out: list[FamilySpec] = []
    out += [orb.ORBSpec(), dataclasses.replace(orb.ORBSpec(), mode="fade")]
    out += [gap.GAPSpec(), dataclasses.replace(gap.GAPSpec(), mode="go")]
    out += [overnight.OVERNIGHTSpec(), dataclasses.replace(overnight.OVERNIGHTSpec(), mode="reverse")]
    out += [volrev.VOLREVSpec(), dataclasses.replace(volrev.VOLREVSpec(), mode="expand", anchor="none")]
    out += [roundnum.ROUNDSpec(), dataclasses.replace(roundnum.ROUNDSpec(), mode="break", hold=3)]
    out += [eod.EODSpec(), dataclasses.replace(eod.EODSpec(), mode="reverse")]
    for leader in leadlag.PAIRS.get(market, ()):
        out.append(dataclasses.replace(leadlag.LEADLAGSpec(), leader=leader))
    canon = [s.canonical() for s in out]
    assert len({s.canonical_hash() for s in canon}) == len(canon), "duplicate specs in selection"
    assert {s.FAMILY for s in canon} <= set(FAMILY_NAMES)
    return canon


def select_specs_phase2(market: str) -> list[FamilySpec]:
    """Fit-free selection for a Phase-2 market (see ``PHASE2_SELECTION_RULE``); no parameter is chosen here."""
    from alpha.families import orb

    if market not in PHASE2_CANONICALS:
        raise ProductionSpecError(f"{market!r} is not a Phase-2 market")
    out: list[FamilySpec] = [orb.ORBSpec(), dataclasses.replace(orb.ORBSpec(), mode="fade")]
    canon = [s.canonical() for s in out]
    assert len({s.canonical_hash() for s in canon}) == len(canon)
    assert all(len(orb.fit(None, s).values) == 0 for s in canon)  # type: ignore[arg-type]  # ORB.fit ignores the data: fit-free
    return canon


def build_v1_1_payload(base_path: str | Path | None = None) -> dict[str, Any]:
    """v1.1 payload = the v1 file's markets/provenance VERBATIM + the fit-free Phase-2 entries, sealed with the
    v1.1 hash. Deterministic: same v1 file -> same v1.1 hash."""
    base = json.loads(Path(base_path or DEFAULT_PATH).read_text(encoding="utf-8"))
    from_payload(base)  # the base must verify as v1 first
    body = {k: v for k, v in base.items() if k not in ("strategy_hash", "schema", "selection_rule")}
    markets = dict(body["markets"])
    prov = dict(body["provenance"])
    for m in PHASE2_CANONICALS:
        markets[m] = [{"spec": s.to_dict(), "thr": []} for s in select_specs_phase2(m)]
        prov[m] = {
            "n_fit_bars": 0, "first_bar_utc": None, "last_bar_utc": None, "last_berlin_date": "",
            "excluded_unfitted": [], "fit_free_families_only": True,
            "note": "no history fitted; thresholds empty by construction (ORB)",
        }
    new_body = {
        **body, "markets": markets, "provenance": prov, "schema": SCHEMA_V1_1,
        "selection_rule": SELECTION_RULE + " || " + PHASE2_SELECTION_RULE,
        "base_strategy_hash_v1": base["strategy_hash"],
    }
    return {**new_body, "strategy_hash": _hash_body(new_body)}


def load_production_spec_for(phase2_markets: tuple[str, ...] = ()) -> ProductionSpecSet:
    """v1 (bit-identical to today) unless a Phase-2 market is enabled, then the v1.1 superset."""
    return load_production_spec(DEFAULT_PATH_V1_1 if phase2_markets else None)


# ------------------------------------------------------------------------------ fit
def _berlin_last_date(frame: pd.DataFrame) -> str:
    ts = pd.DatetimeIndex(frame["ts"]).tz_convert("Europe/Berlin")
    return str(ts.max().date())


def fit_production_specs(
    fit_end: str = DEFAULT_FIT_END,
    *,
    markets: tuple[str, ...] = CANONICALS,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Fit thresholds ONCE and return the (sealed) JSON payload. Never touches data after ``fit_end``."""
    if pd.Timestamp(fit_end) > pd.Timestamp(DEV_END):
        raise ProductionSpecError(f"fit_end {fit_end} is after the development end {DEV_END}")
    mspecs = {m: load_market_spec(m) for m in CANONICALS}
    frames: dict[str, pd.DataFrame] = {}
    for m in CANONICALS:
        frames[m] = dev_frame(load_dev_market_frame(mspecs[m], data_root=data_root), end=fit_end)
    out_markets: dict[str, Any] = {}
    prov: dict[str, Any] = {}
    for m in markets:
        ms = mspecs[m]
        cal = MarketCalendar.from_market_spec(ms)
        leaders = {
            ld: build_leader_features(
                frames[m], frames[ld], MarketCalendar.from_market_spec(mspecs[ld]), ld
            )
            for ld in leadlag.PAIRS.get(m, ())
        }
        train = build_family_data(
            frames[m], cal, name=m, point_size=ms.point_size, tick_size=ms.tick_size,
            asset_class=ms.asset_class, cross=leaders,
        )
        entries: list[dict[str, Any]] = []
        excluded: list[str] = []
        for spec in select_specs(m):
            thr = fit_thresholds(train, spec)
            if not thr.ok:
                excluded.append(strategy_id_of(spec))
                continue
            entries.append({"spec": spec.to_dict(), "thr": [_enc(v) for v in thr.values]})
        out_markets[m] = entries
        prov[m] = {
            "n_fit_bars": len(frames[m]),
            "first_bar_utc": pd.Timestamp(frames[m]["ts"].iloc[0]).isoformat(),
            "last_bar_utc": pd.Timestamp(frames[m]["ts"].iloc[-1]).isoformat(),
            "last_berlin_date": _berlin_last_date(frames[m]),
            "excluded_unfitted": excluded,
        }
    body = {
        "schema": SCHEMA,
        "selection_rule": SELECTION_RULE,
        "fit_end": fit_end,
        "holdout_start": FORWARD_HOLDOUT_START,
        "provenance": prov,
        "markets": out_markets,
    }
    return {**body, "strategy_hash": _hash_body(body)}


def _hash_body(body: Mapping[str, Any]) -> str:
    prefix = "demo-production-spec-1.1:" if body.get("schema") == SCHEMA_V1_1 else "demo-production-spec-1:"
    return hashlib.sha256((prefix + _canon(dict(body))).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------ load / verify
def from_payload(payload: Mapping[str, Any]) -> ProductionSpecSet:
    body = {k: v for k, v in payload.items() if k != "strategy_hash"}
    if body.get("schema") not in (SCHEMA, SCHEMA_V1_1):
        raise ProductionSpecError(f"unknown schema {body.get('schema')!r}")
    allowed = CANONICALS + (PHASE2_CANONICALS if body.get("schema") == SCHEMA_V1_1 else ())
    if payload.get("strategy_hash") != _hash_body(body):
        raise ProductionSpecError("strategy_hash mismatch: production spec file was modified")
    fit_end = str(body["fit_end"])
    if pd.Timestamp(fit_end) >= pd.Timestamp(FORWARD_HOLDOUT_START):
        raise ProductionSpecError(f"fit_end {fit_end} reaches into the forward holdout")
    for m, p in body["provenance"].items():
        if p["last_berlin_date"] > fit_end:
            raise ProductionSpecError(f"{m}: fit bars after fit_end")
    markets: list[tuple[str, tuple[FrozenSpec, ...]]] = []
    for m, entries in body["markets"].items():
        if m not in allowed:
            raise ProductionSpecError(f"unknown market {m}")
        specs: list[FrozenSpec] = []
        for e in entries:
            fam = e["spec"]["family"]
            if fam not in SPEC_CLASSES:
                raise ProductionSpecError(f"unknown family {fam}")
            specs.append(
                FrozenSpec(m, family_spec_from_dict(e["spec"]), tuple(_dec(v) for v in e["thr"]))
            )
        markets.append((m, tuple(specs)))
    return ProductionSpecSet(
        fit_end=fit_end,
        markets=tuple(markets),
        provenance=_canon(body["provenance"]),
        strategy_hash=str(payload["strategy_hash"]),
    )


def load_production_spec(path: str | Path | None = None) -> ProductionSpecSet:
    p = Path(path) if path is not None else DEFAULT_PATH
    return from_payload(json.loads(p.read_text(encoding="utf-8")))


def dump_payload(payload: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True, allow_nan=False) + "\n",
                          encoding="utf-8")


def market_spec_hash(ms: MarketSpec) -> str:
    return hashlib.sha256(_canon(dataclasses.asdict(ms)).encode()).hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit and write the frozen production spec (once).")
    ap.add_argument("--fit-end", default=DEFAULT_FIT_END)
    ap.add_argument("--out", default=str(DEFAULT_PATH))
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists: the production spec is immutable; write a new versioned file")
    payload = fit_production_specs(args.fit_end)
    dump_payload(payload, out)
    print(f"wrote {out} strategy_hash={payload['strategy_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def validate_sim_windows(production: ProductionSpecSet, market: str, mspec: object) -> int:
    """Every frozen spec of ``market`` must have a well-ordered effective SimWindow on ``mspec``'s
    calendar (used by the runner's clock-chain verification; keeps ``alpha`` imports inside the signal
    layer). Returns the number of specs checked; raises ``ValueError`` otherwise."""
    from alpha.families.spec import MarketCalendar

    mcal = MarketCalendar.from_market_spec(mspec)  # type: ignore[arg-type]
    specs = production.specs_for(market)
    for fs in specs:
        w = fs.spec.effective_window(mcal)
        if not (0 <= w.entry_start_min < w.entry_end_min <= w.exit_min <= 1440):
            raise ValueError(f"SimWindow invalid for {fs.strategy_id}")
    return len(specs)
