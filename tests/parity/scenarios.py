"""C6 parity scenarios. Each returns a `ParityResult`; categories are decided HERE, explicitly,
with the reasoning recorded in `detail` (a difference is never silently tolerated).

MATCH                 same economic/safety semantics.
EXPECTED_DIFFERENCE   intentionally different (documented reason).
LEGACY_BUG            legacy violates the intended contract (Nautilus/adapter is right).
NAUTILUS/ADAPTER_BUG  the Nautilus path violates the intended contract.
UNRESOLVED            contract unclear -- must be empty before C7.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from risk.models import ReconciliationState as RS
from risk.models import RuntimeMode as RM
from tests.parity.runners import LegacyRunner, NautilusRunner, Obs

D = Decimal


class Category(StrEnum):
    MATCH = "MATCH"
    EXPECTED_DIFFERENCE = "EXPECTED_DIFFERENCE"
    LEGACY_BUG = "LEGACY_BUG"
    NAUTILUS_ADAPTER_BUG = "NAUTILUS/ADAPTER_BUG"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParityResult:
    name: str
    area: str
    category: Category
    detail: str
    severity: str = "-"  # for bugs: Critical | High | Medium | Low


Scenario = Callable[[pathlib.Path], ParityResult]
TOL = D("0.02")  # Nautilus Money is rounded to the account currency (EUR, 2 dp)


def _close(a: Decimal | None, b: Decimal | None, tol: Decimal = TOL) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


def _pair(tmp: pathlib.Path, *, fee_rate: Decimal = D(0)) -> tuple[LegacyRunner, NautilusRunner]:
    return LegacyRunner(fee_rate=fee_rate), NautilusRunner(tmp)


def _done(nautilus: NautilusRunner) -> None:
    nautilus.close()


def _verdict(name: str, area: str, ok: bool, detail: str) -> ParityResult:
    if ok:
        return ParityResult(name=name, area=area, category=Category.MATCH, detail=detail)
    return ParityResult(
        name=name, area=area, category=Category.UNRESOLVED, detail=f"DIVERGED: {detail}"
    )


ENTRY = [("t1", "25001.50", "1.00")]


def full_fill(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            assert r.place_entry("buy", "1.00", "24900", ENTRY) == "ACCEPTED"
            r.deliver()
        a, b = leg.obs(), nau.obs()
        ok = a.position_qty == b.position_qty == D("1.00") and _close(a.avg_price, b.avg_price)
        return _verdict(
            "entry_full_fill",
            "position quantity",
            ok,
            f"{a.position_qty}@{a.avg_price} vs {b.position_qty}@{b.avg_price}",
        )
    finally:
        _done(nau)


def partial_fills(tmp):
    fills = [("t1", "25001.50", "0.25"), ("t2", "25001.75", "0.25"), ("t3", "25002.00", "0.50")]
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            assert r.place_entry("buy", "1.00", "24900", fills) == "ACCEPTED"
        steps = []
        for _ in range(3):
            leg.deliver(1)
            nau.deliver(1)
            steps.append((leg.obs().position_qty, nau.obs().position_qty))
        a, b = leg.obs(), nau.obs()
        ok = (
            all(x == y for x, y in steps)
            and steps[-1][0] == D("1.00")
            and _close(a.avg_price, b.avg_price, D("0.0001"))
        )
        return _verdict(
            "partial_fills_accumulate",
            "partial fills",
            ok,
            f"steps={steps} avg {a.avg_price} vs {b.avg_price}",
        )
    finally:
        _done(nau)


def duplicate_fills(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver(repeats=4)
        a, b = leg.obs(), nau.obs()
        return _verdict(
            "duplicate_fill_booked_once",
            "duplicate fills",
            a.position_qty == b.position_qty == D("1.00"),
            f"{a.position_qty} vs {b.position_qty}",
        )
    finally:
        _done(nau)


def partial_close(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
            assert r.place_reduce("sell", "0.25", [("c1", "25010.00", "0.25")]) == "ACCEPTED"
            r.deliver()
        a, b = leg.obs(), nau.obs()
        ok = (
            a.position_qty == b.position_qty == D("0.75")
            and _close(a.avg_price, b.avg_price)
            and _close(a.realized_net, b.realized_net)
        )
        return _verdict(
            "partial_close_1.00_to_0.75",
            "partial close / realized PnL",
            ok,
            f"qty {a.position_qty}/{b.position_qty} pnl {a.realized_net}/{b.realized_net}",
        )
    finally:
        _done(nau)


def full_close_pnl(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
            r.place_reduce("sell", "1.00", [("c1", "25010.00", "1.00")])
            r.deliver()
        a, b = leg.obs(), nau.obs()
        ok = a.position_qty == b.position_qty == 0 and _close(a.realized_net, b.realized_net)
        return _verdict(
            "full_close_realized_pnl", "realized PnL", ok, f"{a.realized_net} vs {b.realized_net}"
        )
    finally:
        _done(nau)


def short_symmetry(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("sell", "0.50", "25100", [("t1", "25000.00", "0.50")])
            r.deliver()
            r.place_reduce("buy", "0.50", [("c1", "24990.00", "0.50")])
            r.deliver()
        a, b = leg.obs(), nau.obs()
        ok = (
            a.position_qty == b.position_qty == 0
            and _close(a.realized_net, b.realized_net)
            and a.realized_net > 0
        )
        return _verdict(
            "short_round_trip",
            "position quantity / realized PnL",
            ok,
            f"{a.realized_net} vs {b.realized_net}",
        )
    finally:
        _done(nau)


def fees(tmp):
    rate = D("0.0005")
    leg = LegacyRunner(fee_rate=rate)
    nau = NautilusRunner(tmp)
    try:
        leg.place_entry("buy", "1.00", "24900", ENTRY)
        leg.deliver()
        nau.broker.cfg.commission_per_lot = float(rate * D("25001.50"))
        nau.place_entry("buy", "1.00", "24900", ENTRY)
        nau.deliver()
        leg.place_reduce("sell", "1.00", [("c1", "25010.00", "1.00")])
        leg.deliver()
        nau.broker.cfg.commission_per_lot = float(rate * D("25010.00"))
        nau.place_reduce("sell", "1.00", [("c1", "25010.00", "1.00")])
        nau.deliver()
        a, b = leg.obs(), nau.obs()
        same_value = _close(a.fees, b.fees, D("0.03")) and _close(
            a.realized_net, b.realized_net, D("0.03")
        )
        detail = (
            f"fees {a.fees} vs {b.fees}, net {a.realized_net} vs {b.realized_net}; legacy ESTIMATES fees "
            "from a cost schedule, Nautilus books the BROKER-reported commission (equal only "
            "because this scenario feeds the same amount)"
        )
        if not same_value:
            return ParityResult(
                name="fees_and_net_pnl",
                area="fees",
                category=Category.UNRESOLVED,
                detail=f"DIVERGED: {detail}",
            )
        return ParityResult(
            name="fees_and_net_pnl",
            area="fees",
            category=Category.EXPECTED_DIFFERENCE,
            detail=detail,
        )
    finally:
        _done(nau)


def no_pyramiding(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
        second_leg = leg.place_entry("buy", "1.00", "24900", [("t9", "25002.00", "1.00")])
        second_nau = nau.place_entry("buy", "1.00", "24900", [("t9", "25002.00", "1.00")])
        nau_blocked = second_nau.startswith("BLOCKED") and "POSITION_EXISTS" in second_nau
        if nau_blocked and second_leg == "ACCEPTED":
            return ParityResult(
                name="no_implicit_pyramiding",
                area="one position / no pyramiding",
                category=Category.EXPECTED_DIFFERENCE,
                detail="legacy execution accepts an additional same-side order (the one-position rule lives above it, in strategy/risk); the adapter enforces V1 one-position-per-instrument as defense in depth",
            )
        return _verdict(
            "no_implicit_pyramiding",
            "one position / no pyramiding",
            nau_blocked and second_leg != "ACCEPTED",
            f"legacy={second_leg} nautilus={second_nau}",
        )
    finally:
        _done(nau)


def no_same_tick_flip(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
        flip_leg = leg.place_entry("sell", "2.00", "25100", [("t8", "25000.00", "2.00")])
        flip_nau = nau.place_entry("sell", "2.00", "25100", [("t8", "25000.00", "2.00")])
        oversize_leg = leg.place_reduce("sell", "2.00", [("c9", "25000.00", "2.00")])
        oversize_nau = nau.place_reduce("sell", "2.00", [("c9", "25000.00", "2.00")])
        both_block_oversize = oversize_leg.startswith("BLOCKED") and oversize_nau.startswith(
            "BLOCKED"
        )
        nau_blocks_flip = flip_nau.startswith("BLOCKED")
        if both_block_oversize and nau_blocks_flip and flip_leg == "ACCEPTED":
            return ParityResult(
                name="no_same_tick_flip",
                area="no same-tick flip / reduce-only never over-reduces",
                category=Category.EXPECTED_DIFFERENCE,
                detail="an oversized REDUCE-ONLY order is blocked by both (MATCH); a non-reduce opposite order is accepted by legacy execution but blocked by the adapter (V1 rule enforced at the final admission)",
            )
        return _verdict(
            "no_same_tick_flip",
            "no same-tick flip",
            both_block_oversize and nau_blocks_flip and flip_leg != "ACCEPTED",
            f"flip legacy={flip_leg} nautilus={flip_nau}; oversize legacy={oversize_leg} nautilus={oversize_nau}",
        )
    finally:
        _done(nau)


def bare_entry(tmp):
    leg, nau = _pair(tmp)
    try:
        a = leg.place_entry("buy", "1.00", None, ENTRY)
        b = nau.place_entry("buy", "1.00", None, ENTRY)
        if a == "ACCEPTED" and b.startswith("BLOCKED") and "ENTRY_WITHOUT_ATTACHED_STOP_LOSS" in b:
            return ParityResult(
                name="bare_entry_without_stop",
                area="protective-order behavior",
                category=Category.EXPECTED_DIFFERENCE,
                detail="C6/C7 hard gate: the MT5 adapter refuses any entry without a broker-side stop; legacy paper execution allows it",
            )
        return _verdict(
            "bare_entry_without_stop",
            "protective-order behavior",
            False,
            f"legacy={a} nautilus={b}",
        )
    finally:
        _done(nau)


STATES = [
    (RM.READY, RS.RECONCILED),
    (RM.READY, RS.NOT_RECONCILED),
    (RM.READY, RS.RECONCILING),
    (RM.READY, RS.MISMATCH),
    (RM.HALTED, RS.RECONCILED),
    (RM.HALTED, RS.NOT_RECONCILED),
    (RM.HALTED, RS.MISMATCH),
    (RM.RECONCILING, RS.RECONCILED),
    (RM.RECONCILING, RS.NOT_RECONCILED),
]


def _matrix(tmp, *, reduce_only: bool, name: str) -> ParityResult:
    diffs = []
    for index, (runtime, recon) in enumerate(STATES):
        leg = LegacyRunner()
        nau = NautilusRunner(tmp / f"m{index}")
        try:
            if reduce_only:  # an open position exists before the gate state changes
                for r in (leg, nau):
                    r.place_entry("buy", "1.00", "24900", ENTRY)
                    r.deliver()
            for r in (leg, nau):
                r.set_state(runtime, recon)
            if reduce_only:
                a = leg.place_reduce("sell", "0.25", [("c1", "25010.00", "0.25")])
                b = nau.place_reduce("sell", "0.25", [("c1", "25010.00", "0.25")])
            else:
                a = leg.place_entry("buy", "0.25", "24900", [("t2", "25001.50", "0.25")])
                b = nau.place_entry("buy", "0.25", "24900", [("t2", "25001.50", "0.25")])
            if (a == "ACCEPTED") != (b == "ACCEPTED"):
                diffs.append((runtime.value, recon.value, a, b))
        finally:
            _done(nau)
    area = "new-exposure / reduce-only admission"
    if diffs:
        return ParityResult(
            name=name, area=area, category=Category.UNRESOLVED, detail=f"DIVERGED: {diffs}"
        )
    return ParityResult(
        name=name,
        area=area,
        category=Category.MATCH,
        detail=f"{len(STATES)} runtime x reconciliation states admit/block identically",
    )


def admission_new_exposure(tmp):
    return _matrix(tmp / "ne", reduce_only=False, name="new_exposure_admission_matrix")


def admission_reduce_only(tmp):
    return _matrix(tmp / "ro", reduce_only=True, name="reduce_only_admission_matrix")


def inbound_while_gated(tmp):
    fills = [("t1", "25001.50", "0.50"), ("t2", "25001.75", "0.50")]
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", fills)
            r.deliver(1)
            r.set_state(RM.HALTED, RS.MISMATCH)
            r.deliver(1)  # broker truth arrives while outbound is shut
        a, b = leg.obs(), nau.obs()
        return _verdict(
            "inbound_fill_while_halted_mismatch",
            "inbound broker truth",
            a.position_qty == b.position_qty == D("1.00"),
            f"{a.position_qty} vs {b.position_qty}",
        )
    finally:
        _done(nau)


def unknown_order_fill(tmp):
    leg, nau = (
        LegacyRunner(),
        NautilusRunner(tmp, live_engine=True),
    )  # Nautilus' report reconciliation
    try:
        leg.deliver_unknown("x1", "25000.00", "0.50")
        nau.deliver_unknown("x1", "25000.00", "0.50")
        a, b = leg.obs(), nau.obs()
        both_halt = a.halted and b.halted
        if both_halt and a.position_qty == 0 and b.position_qty != 0:
            return ParityResult(
                name="fill_for_unknown_order",
                area="inbound broker truth",
                category=Category.LEGACY_BUG,
                severity="Medium",
                detail=f"docs/EXECUTION_CONTRACT.md: unknown-order ingestion 'may raise a flag but never drops the event'. Legacy halts and DROPS the fill (position {a.position_qty}); the adapter books it as reported ({b.position_qty}) AND halts. Nautilus behavior is the intended contract; legacy is retired/oracle-only, so it is recorded, not patched.",
            )
        return _verdict(
            "fill_for_unknown_order",
            "inbound broker truth",
            both_halt and a.position_qty == b.position_qty,
            f"legacy {a} nautilus {b}",
        )
    finally:
        _done(nau)


def restart_semantics(tmp):
    results = []
    for held in (False, True):
        leg = LegacyRunner()
        nau = NautilusRunner(tmp / f"r{held}", auto_reconcile=True)
        try:
            if held:
                for r in (leg, nau):
                    r.place_entry("buy", "1.00", "24900", ENTRY)
                    r.deliver()
            leg.restart()
            nau.restart()
            nau.h.client.recon.on_connect(
                nau.h.session.generation
            )  # restarted runtime: nothing compared yet
            not_rec = (not leg.obs().reconciled, not nau.obs().reconciled)
            # A restarted Nautilus cache is empty (no persistence in the harness): the broker still
            # holds the position. Legacy restores its own state from the checkpoint instead.
            a = leg.reconcile()
            b = nau.reconcile()
            results.append((held, not_rec, a, b))
        finally:
            _done(nau)
    unreconciled_after_restart = all(nr == (True, True) for _h, nr, _a, _b in results)
    flat_ok = results[0][2] is True and results[0][3] is True
    held_legacy_ok = results[1][2] is True
    held_nautilus_mismatch = results[1][3] is False
    if unreconciled_after_restart and flat_ok and held_legacy_ok and held_nautilus_mismatch:
        return ParityResult(
            name="restart_recovery",
            area="restart / recovery semantics",
            category=Category.EXPECTED_DIFFERENCE,
            detail="both restart NOT_RECONCILED and both reconcile a flat book to RECONCILED (MATCH). With an open position, legacy restores position state from its own checkpoint, whereas a restarted Nautilus cache is empty until Nautilus' mass-status reconciliation runs (proven in test_restart_reconciles_against_the_real_broker_snapshot...): the adapter correctly reports MISMATCH rather than trusting persisted reconciliation",
        )
    return _verdict("restart_recovery", "restart / recovery semantics", False, str(results))


def mismatch_halts_and_clears(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
        nau.broker.positions_get()[0].volume = 0.75  # broker diverges from local
        bad = (leg.reconcile(D("0.75")), nau.reconcile())
        halted = (leg.obs().halted, nau.obs().halted)
        blocked = (
            leg.place_entry("buy", "0.25", "24900", [("z", "25001.50", "0.25")]).startswith(
                "BLOCKED"
            ),
            nau.place_entry("buy", "0.25", "24900", [("z", "25001.50", "0.25")]).startswith(
                "BLOCKED"
            ),
        )
        nau.broker.positions_get()[0].volume = 1.0
        good = (leg.reconcile(D("1.00")), nau.reconcile())
        ok = (
            bad == (False, False)
            and halted == (True, True)
            and blocked == (True, True)
            and good == (True, True)
        )
        return _verdict(
            "reconciliation_mismatch_halt_and_recovery",
            "reconciliation gates / halt behavior",
            ok,
            f"bad={bad} halted={halted} blocked={blocked} good={good}",
        )
    finally:
        _done(nau)


def protective_lifecycle(tmp):
    leg, nau = _pair(tmp)
    try:
        for r in (leg, nau):
            r.place_entry("buy", "1.00", "24900", ENTRY)
            r.deliver()
        leg_protected = any(
            o.client_order_id.endswith(":stop") and not o.is_terminal()
            for o in leg.engine.orders.values()
        )
        nau_protected = nau.broker.positions_get()[0].sl == 24900.0
        for r in (leg, nau):
            r.place_reduce("sell", "1.00", [("c1", "25010.00", "1.00")])
            r.deliver()
        leg_retired = not any(
            o.client_order_id.endswith(":stop") and not o.is_terminal()
            for o in leg.engine.orders.values()
        )
        from nautilus_trader.model.enums import OrderStatus

        stops = [o for o in nau.h.cache.orders() if o.order_type.name == "STOP_MARKET"]
        nau_retired = nau.broker.positions_get() == () and all(
            o.status is OrderStatus.CANCELED for o in stops
        )
        ok = leg_protected and nau_protected and leg_retired and nau_retired
        if ok:
            return ParityResult(
                name="protective_stop_lifecycle",
                area="protective-order behavior",
                category=Category.EXPECTED_DIFFERENCE,
                detail="both protect the position after the fill and retire protection after the close (MATCH on semantics); representation differs by design: legacy keeps a resting child order in its own engine, the adapter sets the SL on the BROKER position (no second order lifecycle)",
            )
        return _verdict(
            "protective_stop_lifecycle",
            "protective-order behavior",
            False,
            f"legacy prot={leg_protected}/{leg_retired} nautilus prot={nau_protected}/{nau_retired}",
        )
    finally:
        _done(nau)


SCENARIOS: list[Scenario] = [
    full_fill,
    partial_fills,
    duplicate_fills,
    partial_close,
    full_close_pnl,
    short_symmetry,
    fees,
    no_pyramiding,
    no_same_tick_flip,
    bare_entry,
    admission_new_exposure,
    admission_reduce_only,
    inbound_while_gated,
    unknown_order_fill,
    restart_semantics,
    mismatch_halts_and_clears,
    protective_lifecycle,
]

__all__ = ["SCENARIOS", "Category", "Obs", "ParityResult"]
