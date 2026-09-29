"""Quirks of the REAL MetaTrader5 5.0.6231 API found on the live ActivTrades demo terminal during
the C7 preflight (2026-09-29) that a naive fake hides. The fake broker now reproduces them, and
these tests pin the adapter to the forms the real terminal accepts."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import nautilus_mt5


def test_positions_and_orders_filters_are_keyword_only_like_the_real_api(broker):
    assert broker.positions_get("Ger40") is None
    assert broker.last_error() == (-2, "Unnamed arguments not allowed")
    assert broker.positions_get(None, ticket=1) is None
    assert broker.positions_get(symbol="Ger40") == ()
    assert broker.orders_get("Ger40") is None
    assert broker.orders_get(symbol="Ger40") == ()


def test_history_calls_need_a_date_range_or_a_filter_like_the_real_api(broker):
    assert broker.history_orders_get(None, None) is None
    assert broker.last_error() == (-2, "Invalid arguments")
    assert broker.history_deals_get() is None
    now = datetime(2026, 9, 22, tzinfo=UTC)
    assert broker.history_orders_get(now - timedelta(days=1), now) == ()
    assert broker.history_deals_get(now - timedelta(days=1), now) == ()
    assert broker.history_orders_get(ticket=123) is None  # unknown ticket == None, not ()
    assert broker.last_error() == (-2, "Terminal: Invalid params")


def _calls_forwarding_empty_kwargs(path: Path) -> list[int]:
    """Lines where a call forwards *args AND **kwargs unconditionally (not under a conditional)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        has_star = any(isinstance(a, ast.Starred) for a in node.args)
        has_kwargs = any(k.arg is None for k in node.keywords)
        if has_star and has_kwargs:
            cursor = node
            conditional = False
            while cursor in parents:
                cursor = parents[cursor]
                if isinstance(cursor, ast.IfExp):
                    conditional = True
                    break
                if isinstance(cursor, ast.FunctionDef):
                    break
            if not conditional:
                bad.append(node.lineno)
    return bad


def test_session_never_forwards_empty_kwargs_to_a_metatrader5_builtin():
    """Real-API quirk: `builtin(*args, **{})` makes MetaTrader5 reject positional arguments
    ('Unnamed arguments not allowed'), e.g. order_check(request). Both MT5-facing forwarders in
    the session must therefore branch on `kwargs`."""
    session_py = Path(nautilus_mt5.__file__).parent / "session.py"
    assert _calls_forwarding_empty_kwargs(session_py) == []


def test_session_call_and_lane_proxy_drop_empty_kwargs(broker, tmp_path):
    from adapters.config import MT5ConnectionConfig
    from nautilus_mt5.executor import Mt5Executor
    from nautilus_mt5.session import Mt5Session

    seen = []

    class Strict:
        def order_check(self, request):
            seen.append(("order_check", request))
            return object()

    lane = Mt5Executor()
    try:
        session = Mt5Session(
            broker,
            MT5ConnectionConfig(
                login=broker.cfg.login, password="x", server="s", terminal_path="t"
            ),
            lock_path=tmp_path / "l",
            lane=lane,
        )
        session.state = session.state.__class__.CONNECTED
        strict = Strict()
        lane.run_sync(lambda: session.call("order_check", strict.order_check, {"a": 1}))
        assert seen == [("order_check", {"a": 1})]
    finally:
        lane.shutdown()
