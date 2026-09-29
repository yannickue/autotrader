"""ActivTrades/MetaTrader5 CFD adapter.

This package is the ONLY place in this repository that is allowed to speak
the raw `MetaTrader5` module surface. Every other component in `src/` must
depend only on the typed models in `models.py` and the protocol in
`client.py` -- never on `MetaTrader5` directly (directive-mandated: "Core
trading modules must NOT import MetaTrader5 directly. Use
protocols/interfaces.").

No module in this package imports the real `MetaTrader5` package: it is
structurally decoupled via `MT5ClientProtocol` (`client.py`), so every class
here is fully unit-testable with `testing.FakeMT5Client` in an environment
with no MT5 terminal at all (as this one is).
"""
