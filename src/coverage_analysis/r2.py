# ruff: noqa: E501
"""READ-ONLY use of the existing R2 readers (``DemoStore.funnel_rows`` / ``counterfactual_rows`` / ``get_outcome``).

No new SQL, no new tables, no migration. The store is opened on a COPY of the demo database (copy
``demo.sqlite`` + ``-wal`` + ``-shm`` to scratch first); a path inside an ``artifacts`` directory is refused so the
live file can never be opened by accident. Only reader methods are called."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from demo.store import DemoStore

NOT_TRADED_STATES = ("RISK_REJECTED", "SEND_FAILED", "CANCELLED")


@dataclass(frozen=True, slots=True)
class R2Row:
    opportunity_id: str
    market: str
    direction: int
    signal_ts: datetime  # CLOSE of the deciding bar (UTC)
    family: str
    accepted: bool
    reasons: tuple[str, ...]
    intent_state: str | None
    stack_reject_code: str | None
    has_outcome: bool
    outcome_net_r: float | None
    cf_r: float | None
    cf_source: str | None

    @property
    def executed(self) -> bool:
        """Engine-accepted AND an intent exists that did not end in a stack reject / send failure / cancel."""
        return self.accepted and self.intent_state is not None and self.intent_state not in NOT_TRADED_STATES


def rows_from_store(store: DemoStore, phase: str | None = None) -> list[R2Row]:
    cf = {r["opportunity_id"]: r for r in store.counterfactual_rows(phase)}
    out: list[R2Row] = []
    for r in store.funnel_rows(phase):
        if r.get("trade_type", "STRATEGY") != "STRATEGY" or r["direction"] is None:
            continue
        net = None
        if r["intent_id"] is not None and r["has_outcome"]:
            o = store.get_outcome(r["intent_id"])
            net = None if o is None else float(o.net_r)
        c = cf.get(r["opportunity_id"])
        out.append(R2Row(
            opportunity_id=r["opportunity_id"], market=r["market"], direction=int(r["direction"]),
            signal_ts=datetime.fromisoformat(r["signal_ts"]), family=str(r["family"] or ""),
            accepted=bool(r["accepted"]), reasons=tuple(r["reasons"]), intent_state=r["state"],
            stack_reject_code=r["stack_reject_code"], has_outcome=r["has_outcome"], outcome_net_r=net,
            cf_r=None if c is None else float(c["r"]), cf_source=None if c is None else c["source"],
        ))
    out.sort(key=lambda x: x.signal_ts)
    return out


def _guard(db_copy: str | Path) -> Path:
    p = Path(db_copy).resolve()
    if "artifacts" in {part.lower() for part in p.parts}:
        raise ValueError(f"{p}: refusing to open a file inside an 'artifacts' directory; pass a COPY")
    return p


def load_r2(db_copy: str | Path, phase: str | None = None) -> list[R2Row]:
    with DemoStore(_guard(db_copy)) as st:
        return rows_from_store(st, phase)


def funnel_summary(db_copy: str | Path) -> dict[str, Any]:
    """The existing rejection-funnel summary (``demo.funnel.funnel``) of the copy, for the report header."""
    from demo.funnel import funnel

    with DemoStore(_guard(db_copy)) as st:
        return funnel(st)["summary"]
