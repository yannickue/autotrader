# ruff: noqa: E501
"""Statistics of the observer lab (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

* Effect size = P(outcome | event) - P(outcome | matched control).
* Uncertainty = DAY-BLOCK bootstrap: whole local days (events AND controls of a day together) are resampled with replacement (seeded,
  ``B`` configurable). Intraday observations of one day are strongly dependent; resampling single events would be anti-conservative.
  Percentile interval; the two-sided bootstrap p-value is the share of resamples on the other side of 0 (consistent with the CI).
* Single rates: Wilson interval (``coverage_analysis.control.wilson`` is reused, not re-implemented).
* Predeclared minimum evidence (``DEFAULT_MIN_EVIDENCE``): fewer events/controls/day-blocks/clusters => ``INSUFFICIENT_EVIDENCE``
  (the numbers are still reported, but no verdict and no adjusted p-value is attached).
* Multiple testing: Holm (family-wise) and Benjamini-Hochberg (FDR) over a REGISTERED hypothesis list. ``HypothesisRegistry`` counts
  every registered hypothesis (also those not evaluable) and refuses results for unregistered ones. It can be PERSISTED (JSON file): a
  hypothesis registered by an earlier run cannot be registered again, and ``n_hypotheses`` is the number ever tested. Hypotheses are grouped
  in predeclared FAMILIES (small, declared before looking at data); Holm / BH run within a family (``m`` = family size) and, for reference,
  over the whole registry.
* P-value resolution: a bootstrap p-value cannot be smaller than ``2/(B+1)``. ``choose_B`` takes ``B >= 20 m / alpha`` (capped, with a
  warning); ``is_power_limited`` flags families in which ``m * 2/(B+1) >= alpha`` (nothing can ever be significant: status
  ``POWER_LIMITED`` instead of a misleading ``NOT_SIGNIFICANT``). The block-level standard error gives a normal-approximation p as a check.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from coverage_analysis.control import (
    wilson as wilson_interval,  # noqa: F401  (re-exported on purpose)
)

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NOT_SIGNIFICANT = "NOT_SIGNIFICANT"
SIGNIFICANT_ADJUSTED = "SIGNIFICANT_ADJUSTED"  # adjusted p < alpha AND the block-bootstrap CI excludes 0. Still NOT an edge claim.
POWER_LIMITED = "POWER_LIMITED"  # even the smallest attainable p-value cannot pass the corrected threshold: a "null" here is an artefact, not a finding
OK = "OK"
DEFAULT_ALPHA = 0.05
DEFAULT_B = 2000


@dataclass(frozen=True)
class MinEvidence:
    """Predeclared before looking at results (n per cell == ``demo.entry_exit_quality.SMALL_N``, clusters == ``MIN_CLUSTERS``)."""

    min_events: int = 30
    min_controls: int = 30
    min_blocks: int = 20  # independent local days
    min_clusters: int = 20  # independent event clusters (structure_event_id), checked only when cluster ids exist


DEFAULT_MIN_EVIDENCE = MinEvidence()


STATS_LEGACY = "observer-stats-1"  # blocks: union of both arms; controls blocked by their OWN decision day
STATS_V2 = "observer-stats-2"  # blocks counted PER ARM (>= min_blocks in the event arm AND in the control arm); controls blocked by the day of their EVENT
# observer-stats-3 = stats-2 (per-arm counts, controls take the block of their EVENT) with CONTIGUOUS blocks of >= MIN_BLOCK_TRADING_DAYS trading days instead of single days.
# Why: a control sits up to +-10 trading days from its event (observer-controls-3) and controls of neighbouring events share label windows; a single-day (or
# single-week) block therefore is NOT an independent unit. Blocks of >= 21 trading days (= 2 x 10 + 1) leave dependence only at the block edges.
STATS_V3 = "observer-stats-3"
MIN_BLOCK_TRADING_DAYS = 21
STATS_VERSIONS = (STATS_LEGACY, STATS_V2, STATS_V3)
PER_ARM_STATS = (STATS_V2, STATS_V3)  # versions that count blocks per arm


def contiguous_day_blocks(days, block_len: int = MIN_BLOCK_TRADING_DAYS) -> dict:
    """observer-stats-3 block assignment: map every distinct day ordinal to a block id. The distinct days are sorted and cut into consecutive runs of ``block_len``
    trading days (the days present in the data); a trailing remainder shorter than ``block_len`` is merged into the previous block, so EVERY block holds
    >= ``block_len`` days (a single block when there are fewer days). The mapping depends on the days only, never on a cell, a label or an arm. Days that
    contain no event but lie between two event days only make a block longer in calendar time, never shorter in trading days."""
    if block_len < MIN_BLOCK_TRADING_DAYS:
        raise ValueError(f"observer-stats-3 needs blocks of >= {MIN_BLOCK_TRADING_DAYS} trading days, got {block_len}")
    uniq = np.unique(np.asarray(days))
    n_blocks = max(1, len(uniq) // block_len)
    return {d: min(i // block_len, n_blocks - 1) for i, d in enumerate(uniq.tolist())}


def evidence_status(
    *, n_event: int, n_control: int, n_blocks: int, n_clusters: int | None = None, min_evidence: MinEvidence = DEFAULT_MIN_EVIDENCE,
    n_blocks_event: int | None = None, n_blocks_control: int | None = None,
) -> str:
    """``n_blocks_event`` / ``n_blocks_control`` (observer-stats-2): independent blocks with at least one non-NaN outcome IN THE CELL, separately per
    arm; when given they replace the union count ``n_blocks`` (an arm with few blocks can no longer hide behind the other arm's blocks)."""
    if n_blocks_event is not None and n_blocks_control is not None:
        blocks_short = min(n_blocks_event, n_blocks_control) < min_evidence.min_blocks
    else:
        blocks_short = n_blocks < min_evidence.min_blocks
    if n_event < min_evidence.min_events or n_control < min_evidence.min_controls or blocks_short:
        return INSUFFICIENT_EVIDENCE
    if n_clusters is not None and n_clusters < min_evidence.min_clusters:
        return INSUFFICIENT_EVIDENCE
    return OK


def final_status(status: str, adjusted_p: float | None, ci_low: float, ci_high: float, alpha: float = DEFAULT_ALPHA, *, power_limited: bool = False) -> str:
    if status == INSUFFICIENT_EVIDENCE:
        return INSUFFICIENT_EVIDENCE
    if adjusted_p is not None and adjusted_p < alpha and (ci_low > 0 or ci_high < 0):
        return SIGNIFICANT_ADJUSTED
    if power_limited:
        return POWER_LIMITED
    return NOT_SIGNIFICANT


# ---------------------------------------------------------------------------------------------- p-value resolution / power
B_SAFETY = 20.0  # B >= B_SAFETY * m / alpha  =>  the smallest Holm-adjusted p is at most alpha / B_SAFETY * 2
DEFAULT_B_MAX = 20_000


def p_floor(B: int) -> float:
    """Smallest two-sided bootstrap p-value attainable with ``B`` resamples: 2 / (B + 1)."""
    return 2.0 / (B + 1)


def required_B(m: int, alpha: float = DEFAULT_ALPHA, safety: float = B_SAFETY) -> int:
    return math.ceil(safety * max(1, m) / alpha)


def is_power_limited(m: int, B: int, alpha: float = DEFAULT_ALPHA) -> bool:
    """True when the best conceivable p (the floor) times the family size cannot get below alpha: Holm / BH can NEVER reject in this family."""
    return p_floor(B) * max(1, m) >= alpha


@dataclass(frozen=True)
class BChoice:
    B: int
    required: int  # B_SAFETY * m / alpha
    capped: bool  # the cap (``b_max``) prevented reaching ``required``
    resolution_limited: bool  # with the chosen B no result of this family can reach significance (m * 2/(B+1) >= alpha)
    p_floor: float
    m: int
    warning: str | None = None


def choose_B(m: int, alpha: float = DEFAULT_ALPHA, *, requested: int | None = None, b_min: int = 2000, b_max: int = DEFAULT_B_MAX) -> BChoice:
    """Bootstrap draws for a family of ``m`` hypotheses. ``requested`` (explicit B) is honoured as is. Otherwise B = clip(20 m / alpha, b_min, b_max);
    if the cap binds the choice carries a warning and, when the family can then never reach significance, ``resolution_limited``."""
    req = required_B(m, alpha)
    B = int(requested) if requested is not None else int(min(max(req, b_min), b_max))
    capped = req > B
    limited = is_power_limited(m, B, alpha)
    warn = None
    if capped:
        warn = f"B={B} < required {req} for m={m}, alpha={alpha}: smallest attainable p={p_floor(B):.2e}" + ("; NO result can reach significance (resolution_limited)" if limited else "")
    return BChoice(B, req, capped, limited and capped, p_floor(B), m, warn)


def normal_p(delta: float, se: float) -> float:
    """Two-sided normal-approximation p from a block-bootstrap standard error (unlimited resolution; use next to the bootstrap p as a check)."""
    if not (se > 0) or math.isnan(delta):
        return float("nan")
    return math.erfc(abs(delta) / se / math.sqrt(2.0))


# ---------------------------------------------------------------------------------------------- block bootstrap
@dataclass(frozen=True)
class DeltaEstimate:
    n_event: int
    n_control: int
    k_event: int
    k_control: int
    p_event: float
    p_control: float
    delta: float
    ci_low: float
    ci_high: float
    p_boot: float
    n_blocks: int
    n_clusters: int | None
    B: int
    alpha: float
    se: float = float("nan")  # standard deviation of the bootstrap draws (block-level standard error)
    p_norm: float = float("nan")  # normal-approximation p from delta / se (unlimited resolution)
    n_blocks_event: int | None = None  # blocks with >= 1 non-NaN event outcome (the event arm of THIS cell)
    n_blocks_control: int | None = None  # blocks with >= 1 non-NaN control outcome (the control arm of THIS cell)
    n_nan_draws: int = 0  # bootstrap draws that were NaN (an empty arm in a resample): COUNTED here, never silently dropped without a trace


Arm = tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]  # (y_event, day_event, y_control, day_control)


def _clean(y: np.ndarray, day: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    y = np.asarray(y, dtype=float)
    day = np.asarray(day)
    if len(y) != len(day):
        raise ValueError("outcome and day arrays differ in length")
    keep = ~np.isnan(y)
    return y[keep], day[keep], keep


def _arm_counts(arm: Arm, day_index: dict, D: int) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    out = np.zeros((D, 4))
    ns = []
    for col, (y, d) in enumerate(((arm[0], arm[1]), (arm[2], arm[3]))):
        yy, dd, _ = _clean(y, d)
        idx = np.fromiter((day_index[x] for x in dd), dtype=np.int64, count=len(dd))
        out[:, 2 * col] = np.bincount(idx, weights=yy, minlength=D)  # successes
        out[:, 2 * col + 1] = np.bincount(idx, minlength=D)  # n
        ns.append((int(yy.sum()), len(yy)))
    return out, (ns[0][0], ns[0][1], ns[1][0], ns[1][1])


def _rate(k: np.ndarray, n: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n > 0, k / n, np.nan)


def _bootstrap_sums(counts: np.ndarray, B: int, seed: int, chunk: int = 200):
    D = counts.shape[0]
    rng = np.random.default_rng(seed)
    for lo in range(0, B, chunk):
        m = min(chunk, B - lo)
        idx = rng.integers(0, D, size=(m, D))
        yield counts[idx].sum(axis=1)  # (m, columns)


def _summarise(draws: np.ndarray, alpha: float) -> tuple[float, float, float, float, int]:
    """(ci_low, ci_high, p, se, n_nan_draws): NaN draws are excluded from the summary but their number is returned (a consumer may abort on > 0)."""
    n_nan = int(np.isnan(draws).sum())
    d = draws[~np.isnan(draws)]
    if len(d) == 0:
        return float("nan"), float("nan"), float("nan"), float("nan"), n_nan
    lo, hi = np.percentile(d, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    le, ge = int((d <= 0).sum()), int((d >= 0).sum())
    p = min(1.0, 2.0 * (min(le, ge) + 1) / (len(d) + 1))
    se = float(d.std(ddof=1)) if len(d) > 1 else float("nan")
    return float(lo), float(hi), float(p), se, n_nan


def _n_blocks(y: np.ndarray, d: np.ndarray) -> int:
    """Independent blocks with at least one non-NaN outcome in this arm."""
    _, dd, _ = _clean(y, d)
    return len(np.unique(dd)) if len(dd) else 0


def block_bootstrap_delta(
    y_event, day_event, y_control, day_control, *, B: int = DEFAULT_B, seed: int = 0, alpha: float = DEFAULT_ALPHA,
    cluster_event=None,
) -> DeltaEstimate:
    """delta = P(y|event) - P(y|control); y in {0, 1, NaN}: NaN (censored / undefined) outcomes are excluded from both rates."""
    arm: Arm = (np.asarray(y_event, float), np.asarray(day_event), np.asarray(y_control, float), np.asarray(day_control))
    days = np.unique(np.concatenate([_clean(arm[0], arm[1])[1], _clean(arm[2], arm[3])[1]])) if (len(arm[0]) + len(arm[2])) else np.array([])
    index = {d: i for i, d in enumerate(days.tolist())}
    D = len(days)
    counts, (ke, ne, kc, nc) = _arm_counts(arm, index, D)
    pe = ke / ne if ne else float("nan")
    pc = kc / nc if nc else float("nan")
    draws = np.concatenate([_rate(s[:, 0], s[:, 1]) - _rate(s[:, 2], s[:, 3]) for s in _bootstrap_sums(counts, B, seed)]) if D else np.array([])
    lo, hi, p, se, n_nan = _summarise(draws, alpha)
    ncl = None
    if cluster_event is not None:
        cl = np.asarray(cluster_event)
        ncl = len(set(cl[~np.isnan(arm[0])].tolist()))
    return DeltaEstimate(ne, nc, ke, kc, pe, pc, pe - pc, lo, hi, p, D, ncl, B, alpha, se, normal_p(pe - pc, se), _n_blocks(arm[0], arm[1]), _n_blocks(arm[2], arm[3]), n_nan)


def block_bootstrap_contrast(arm_a: Arm, arm_b: Arm, *, B: int = DEFAULT_B, seed: int = 0, alpha: float = DEFAULT_ALPHA) -> DeltaEstimate:
    """(delta of arm A) - (delta of arm B), whole days resampled JOINTLY for both arms (paired: arm A is typically a subset of arm B)."""
    days = np.unique(np.concatenate([_clean(a[0], a[1])[1] for a in (arm_a, arm_b)] + [_clean(a[2], a[3])[1] for a in (arm_a, arm_b)]))
    index = {d: i for i, d in enumerate(days.tolist())}
    D = len(days)
    ca, (ke, ne, kc, nc) = _arm_counts(arm_a, index, D)
    cb, (kbe, nbe, kbc, nbc) = _arm_counts(arm_b, index, D)
    counts = np.concatenate([ca, cb], axis=1)
    draws = np.concatenate([
        (_rate(s[:, 0], s[:, 1]) - _rate(s[:, 2], s[:, 3])) - (_rate(s[:, 4], s[:, 5]) - _rate(s[:, 6], s[:, 7])) for s in _bootstrap_sums(counts, B, seed)
    ])
    lo, hi, p, se, n_nan = _summarise(draws, alpha)
    pe, pc = (ke / ne if ne else float("nan")), (kc / nc if nc else float("nan"))
    pbe, pbc = (kbe / nbe if nbe else float("nan")), (kbc / nbc if nbc else float("nan"))
    delta = (pe - pc) - (pbe - pbc)
    # per-arm blocks of the CELL arm A (``n_blocks`` stays the union of all four arrays that the bootstrap resamples)
    return DeltaEstimate(ne, nc, ke, kc, pe, pc, delta, lo, hi, p, D, None, B, alpha, se, normal_p(delta, se), _n_blocks(arm_a[0], arm_a[1]), _n_blocks(arm_a[2], arm_a[3]), n_nan)


def block_bootstrap_ci(
    arrays: Sequence[np.ndarray], day: np.ndarray, stat: Callable[..., float], *, B: int = 1000, seed: int = 0, alpha: float = DEFAULT_ALPHA,
) -> tuple[float, float, np.ndarray]:
    """Generic day-block bootstrap of ``stat(*arrays)`` (rows of one day always stay together). Returns (lo, hi, draws)."""
    day = np.asarray(day)
    arrays = [np.asarray(a) for a in arrays]
    order = np.argsort(day, kind="stable")
    sorted_day = day[order]
    _, start = np.unique(sorted_day, return_index=True)
    groups = np.split(order, start[1:])
    rng = np.random.default_rng(seed)
    draws = np.empty(B)
    for b in range(B):
        pick = rng.integers(0, len(groups), size=len(groups))
        idx = np.concatenate([groups[k] for k in pick])
        try:
            draws[b] = stat(*(a[idx] for a in arrays))
        except (ValueError, FloatingPointError, ZeroDivisionError):
            draws[b] = np.nan
    d = draws[~np.isnan(draws)]
    if len(d) == 0:
        return float("nan"), float("nan"), draws
    lo, hi = np.percentile(d, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(lo), float(hi), draws


# ---------------------------------------------------------------------------------------------- multiple testing
def holm_adjust(p: Sequence[float], m: int | None = None) -> list[float]:
    """Holm step-down adjusted p-values. ``m`` = size of the registered family (>= len(p); unevaluated hypotheses count as p = 1)."""
    arr = np.asarray(p, float)
    m = len(arr) if m is None else m
    if m < len(arr):
        raise ValueError("m must be >= the number of p-values")
    order = np.argsort(arr, kind="stable")
    adj = np.empty(len(arr))
    running = 0.0
    for rank, k in enumerate(order):
        running = max(running, (m - rank) * arr[k])
        adj[k] = min(1.0, running)
    return adj.tolist()


def bh_adjust(p: Sequence[float], m: int | None = None) -> list[float]:
    """Benjamini-Hochberg adjusted p-values (FDR); ``m`` as in ``holm_adjust``."""
    arr = np.asarray(p, float)
    m = len(arr) if m is None else m
    if m < len(arr):
        raise ValueError("m must be >= the number of p-values")
    order = np.argsort(arr, kind="stable")
    adj = np.empty(len(arr))
    running = 1.0
    for rank in range(len(arr) - 1, -1, -1):
        k = order[rank]
        running = min(running, m * arr[k] / (rank + 1))
        adj[k] = min(1.0, running)
    return adj.tolist()


DEFAULT_FAMILY = "_run"  # hypotheses registered without an explicit family form one family (the previous whole-registry behaviour)
REGISTRY_FORMAT = 1


class HypothesisReuseError(ValueError):
    """A hypothesis (or family) that is already in the registry - possibly from an EARLIER run - was registered again."""


@dataclass
class HypothesisRegistry:
    """Every hypothesis must be registered BEFORE its result is recorded; the count is the multiplicity.

    * **Persistent** when ``path`` is given: the registry is a JSON file (written atomically on every registration / ``flush``) and is loaded
      again by the next run. Registering a hypothesis that an earlier run already registered is an error (``HypothesisReuseError``): there
      are no best-of-N reruns. ``n_hypotheses`` is therefore the number of hypotheses EVER tested through this registry, across runs.
    * **Families**: a family is a set of hypotheses declared together (``declare_family``) BEFORE the data is looked at, e.g. one family per
      feature group and label with a small predeclared list of contrasts. Holm / BH are applied WITHIN a family with ``m = family size``
      (``adjust(scope="family")``, default) or over everything ever registered (``scope="registry"``). The declaration time and the number
      of results already recorded at that moment are stored (``family_info``): a family declared after results exist is visibly post hoc.
      Hypotheses registered without a family form the single family ``_run`` (the previous whole-registry behaviour).
    * In-memory (``path=None``) behaves exactly as before.
    """

    name: str
    path: str | Path | None = None
    _names: list[str] = field(default_factory=list)
    _p: dict[str, float | None] = field(default_factory=dict)
    _family: dict[str, str] = field(default_factory=dict)
    _families: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path is not None and Path(self.path).exists():
            self._load()

    # ------------------------------------------------------------------------------------------ persistence
    def _load(self) -> None:
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        if data.get("format") != REGISTRY_FORMAT:
            raise ValueError(f"unknown registry format {data.get('format')!r} in {self.path}")
        if data.get("name") != self.name:
            raise ValueError(f"registry file {self.path} belongs to {data.get('name')!r}, not {self.name!r}")
        self._names = list(data["hypotheses"])
        self._family = {h: v["family"] for h, v in data["hypotheses"].items()}
        self._p = {h: v["p"] for h, v in data["hypotheses"].items() if v.get("recorded")}
        self._families = dict(data.get("families", {}))

    def flush(self) -> None:
        """Write the registry (no-op for an in-memory registry). Atomic: temp file + replace."""
        if self.path is None:
            return
        body = {
            "format": REGISTRY_FORMAT, "name": self.name, "n_hypotheses_ever": len(self._names), "families": self._families,
            "hypotheses": {h: {"family": self._family[h], "p": self._p.get(h), "recorded": h in self._p} for h in self._names},
        }
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=False), encoding="utf-8")
        os.replace(tmp, target)

    # ------------------------------------------------------------------------------------------ registration
    def declare_family(self, family: str, *, definition: str = "") -> None:
        """Declare a family BEFORE evaluating it. ``definition`` = free text / hash of the predeclared contrasts. Declaring the same family twice is an error."""
        if family in self._families:
            raise HypothesisReuseError(f"family already declared: {family}")
        self._families[family] = {
            "declared_utc": datetime.now(UTC).isoformat(timespec="seconds"), "definition": definition, "n_results_recorded_at_declaration": len(self._p),
        }

    def register(self, hypothesis: str, family: str | None = None) -> None:
        self._register(hypothesis, family)
        self.flush()

    def _register(self, hypothesis: str, family: str | None) -> None:
        if hypothesis in self._family:
            raise HypothesisReuseError(f"hypothesis already registered (this run or an earlier one): {hypothesis}")
        fam = family or DEFAULT_FAMILY
        if fam not in self._families:
            self.declare_family(fam)
        self._names.append(hypothesis)
        self._family[hypothesis] = fam

    def register_many(self, hypotheses: Sequence[str], family: str | Sequence[str] | None = None) -> None:
        """Register all or none (a duplicate leaves the registry untouched). ``family``: one name for all, or one per hypothesis."""
        fams = [family] * len(hypotheses) if family is None or isinstance(family, str) else list(family)
        if len(fams) != len(hypotheses):
            raise ValueError("one family per hypothesis expected")
        dup = [h for h in hypotheses if h in self._family] + [h for h in set(hypotheses) if list(hypotheses).count(h) > 1]
        if dup:
            raise HypothesisReuseError(f"hypothesis already registered (this run or an earlier one): {dup[0]}")
        for h, f in zip(hypotheses, fams, strict=True):
            self._register(h, f)
        self.flush()

    def record(self, hypothesis: str, p: float | None) -> None:
        if hypothesis not in self._family:
            raise KeyError(f"result for an unregistered hypothesis: {hypothesis}")
        if hypothesis in self._p:
            raise ValueError(f"result already recorded: {hypothesis}")
        self._p[hypothesis] = None if p is None or np.isnan(p) else float(p)

    # ------------------------------------------------------------------------------------------ counts
    @property
    def n_hypotheses(self) -> int:
        """Hypotheses EVER registered (across runs when persistent)."""
        return len(self._names)

    @property
    def n_evaluated(self) -> int:
        return sum(v is not None for v in self._p.values())

    @property
    def n_families(self) -> int:
        return len(self._families)

    def family_of(self, hypothesis: str) -> str:
        return self._family[hypothesis]

    def family_size(self, family: str) -> int:
        return sum(f == family for f in self._family.values())

    def family_info(self, family: str) -> Mapping[str, object]:
        return {**self._families[family], "n_hypotheses": self.family_size(family)}

    # ------------------------------------------------------------------------------------------ correction
    def adjust(self, method: str = "holm", scope: str = "family") -> dict[str, float | None]:
        """Adjusted p per hypothesis. ``scope="family"``: within each family, m = its size (predeclared small families); ``scope="registry"``:
        one family of everything ever registered. Unevaluated members still count in m."""
        fn = {"holm": holm_adjust, "bh": bh_adjust}.get(method)
        if fn is None:
            raise ValueError("method must be 'holm' or 'bh'")
        if scope not in ("family", "registry"):
            raise ValueError("scope must be 'family' or 'registry'")
        out: dict[str, float | None] = {n: None for n in self._names}
        groups: dict[str, list[str]] = {}
        for n in self._names:
            groups.setdefault(self._family[n] if scope == "family" else "*", []).append(n)
        for members in groups.values():
            names = [n for n in members if self._p.get(n) is not None]
            if names:
                out.update(zip(names, fn([self._p[n] for n in names], m=len(members)), strict=True))
        return out

    def summary(self) -> Mapping[str, int | str]:
        return {"registry": self.name, "n_hypotheses": self.n_hypotheses, "n_evaluated": self.n_evaluated, "n_families": self.n_families}


# ---------------------------------------------------------------------------------------------- result record
@dataclass(frozen=True)
class EnrichmentResult:
    feature: str  # column (f_<group>__<name>) or ablation arm description
    group: str | None
    label: str  # y_* column
    cell: str  # cell definition (quantile range / category / conjunction)
    n_event: int
    n_control: int
    p_event: float
    p_control: float
    delta: float  # kind == "single": P(y|event) - P(y|control); "incremental": delta(base + cell) - delta(base)
    ci_low: float
    ci_high: float
    n_blocks: int
    adjusted_p: float | None
    status: str
    versions: Mapping[str, str]
    n_clusters: int | None = None
    p_boot: float | None = None
    kind: str = "single"
    base_delta: float | None = None
    hypothesis: str | None = None
    contrast: str = "event_vs_same_cell_controls"  # or "lift_within_cell" (own matched controls, feature of the controls ignored) / "incremental"
    family: str | None = None
    m_family: int | None = None  # size of the correction family
    n_hypotheses_ever: int | None = None  # registry size across ALL runs
    adjusted_p_registry: float | None = None  # adjusted over everything ever registered
    p_norm: float | None = None  # block-SE normal approximation (check / alternative p)
    n_blocks_event: int | None = None  # per-arm blocks of the cell (observer-stats-2)
    n_blocks_control: int | None = None
    n_nan_draws: int = 0
    stats_version: str | None = None
    p_floor: float | None = None  # smallest attainable bootstrap p
    B: int | None = None
    power_limited: bool = False  # m_family * p_floor >= alpha: no result of this family could ever be significant
    resolution_limited: bool = False  # ... and the B cap is what prevents it
    partition: str | None = None
    purpose: str | None = None
