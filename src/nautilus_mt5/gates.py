"""Outbound admission gates (C2.1 semantics, pure and stateless).

OUTBOUND actions initiated by us are gated here. INBOUND broker truth (fills,
cancels, rejects, deals, position/order reports) is NEVER gated: it is always
ingested, in every runtime/reconciliation state (enforced in the execution
client, which does not call this module for inbound events).

    NEW_EXPOSURE           READY  + RECONCILED(VENUE_SNAPSHOT)   and no unprotected position
    REDUCE_ONLY            RECONCILED(VENUE_SNAPSHOT) and runtime in {READY, HALTED}; OR, for a
                           broker-verified OWN position (fresh positions_get in the same lane
                           call, opposite side, volume <= position volume, ticket bound), ANY
                           runtime/reconciliation state (it can only reduce exposure - the flatten
                           of last resort must not be blockable by a reconciliation MISMATCH)
    PROTECT_TIGHTEN        broker-verified position ticket; any state (it only reduces risk)
    PROTECT_LOOSEN_REMOVE  READY + RECONCILED(VENUE_SNAPSHOT)   (it increases risk)
    CANCEL_UNFILLED        any state (removes not-yet-filled exposure)

`RECONCILED` is only meaningful with source VENUE_SNAPSHOT: a PAPER_SELF_CHECK can
never grant authority over a real broker account.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from risk.models import ReconciliationSource, ReconciliationState, RuntimeMode


class OutboundKind(StrEnum):
    NEW_EXPOSURE = "NEW_EXPOSURE"
    REDUCE_ONLY = "REDUCE_ONLY"
    PROTECT_TIGHTEN = "PROTECT_TIGHTEN"
    PROTECT_LOOSEN_REMOVE = "PROTECT_LOOSEN_REMOVE"
    CANCEL_UNFILLED = "CANCEL_UNFILLED"


@dataclass(frozen=True, slots=True, kw_only=True)
class AdapterStatus:
    runtime: RuntimeMode
    reconciliation: ReconciliationState
    source: ReconciliationSource | None
    unprotected_positions: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Admission:
    ok: bool
    reason: str = "ok"


def _venue_reconciled(status: AdapterStatus) -> bool:
    return (
        status.reconciliation is ReconciliationState.RECONCILED
        and status.source is ReconciliationSource.VENUE_SNAPSHOT
    )


def admit(
    kind: OutboundKind, status: AdapterStatus, *, position_verified: bool = False
) -> Admission:
    if kind is OutboundKind.CANCEL_UNFILLED:
        return Admission(ok=True)
    if kind is OutboundKind.PROTECT_TIGHTEN:
        if not position_verified:
            return Admission(ok=False, reason="PROTECT_NEEDS_BROKER_VERIFIED_POSITION")
        return Admission(ok=True)
    if kind is OutboundKind.REDUCE_ONLY and position_verified:
        return Admission(ok=True)
    if not _venue_reconciled(status):
        return Admission(
            ok=False,
            reason=f"NOT_VENUE_RECONCILED(state={status.reconciliation.value}, "
            f"source={status.source.value if status.source else None})",
        )
    if kind is OutboundKind.REDUCE_ONLY:
        if status.runtime not in (RuntimeMode.READY, RuntimeMode.HALTED):
            return Admission(ok=False, reason=f"RUNTIME_{status.runtime.value.upper()}")
        return Admission(ok=True)
    # NEW_EXPOSURE and PROTECT_LOOSEN_REMOVE
    if status.runtime is not RuntimeMode.READY:
        return Admission(ok=False, reason=f"RUNTIME_{status.runtime.value.upper()}")
    if kind is OutboundKind.NEW_EXPOSURE and status.unprotected_positions:
        return Admission(ok=False, reason="UNPROTECTED_POSITION_AT_BROKER")
    return Admission(ok=True)
