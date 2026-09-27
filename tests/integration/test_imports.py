def test_primary_engine_imports() -> None:
    import nautilus_trader

    assert nautilus_trader.__version__ == "1.231.0"


def test_contract_modules_import() -> None:
    from data.models import MarketSnapshot
    from execution.models import ExecutionRequest
    from risk.models import RiskDecision
    from signals.models import Signal

    assert all((MarketSnapshot, Signal, RiskDecision, ExecutionRequest))

