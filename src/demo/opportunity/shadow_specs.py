# ruff: noqa: E501
"""Hash-sealed spec set of the SHADOW universe (Lane U2): fit-free STRUCT class defaults, see ``demo.shadow_universe``.

Lives in ``demo.opportunity`` because only that package may import the frozen alpha family kernels
(isolation test ``test_demo_opportunity_imports_only_frozen_alpha_kernels``)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from demo.opportunity.production_spec import (
    ROLE_PRIMARY,
    ROLE_SHADOW,
    FrozenSpec,
    ProductionSpecSet,
)


def shadow_production_set(markets: Iterable[str]) -> ProductionSpecSet:
    """Hash-sealed spec set for the shadow universe: STRUCT class defaults (see ``SHADOW_FAMILY_POLICY``)."""
    import dataclasses as dc

    from alpha.families import structbrk

    base = structbrk.STRUCTSpec()
    variants = [(dc.replace(base, mode="confirmed"), ROLE_PRIMARY)] + [
        (dc.replace(base, mode=m), ROLE_SHADOW) for m in ("breakout", "retest", "fade")
    ]
    markets_t: list[tuple[str, tuple[FrozenSpec, ...]]] = []
    body: list[Any] = []
    for m in sorted(set(markets)):
        fss = tuple(FrozenSpec(market=m, spec=sp, thr_values=(), role=role) for sp, role in variants)
        markets_t.append((m, fss))
        body.append([m, [[sp.to_dict(), role] for sp, role in variants]])
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
    return ProductionSpecSet(
        fit_end="none", markets=tuple(markets_t), provenance="shadow-universe: no history fitted; STRUCT placeholders",
        strategy_hash="shadow-struct-" + digest,
    )
