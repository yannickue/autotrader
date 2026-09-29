"""Nautilus <-> ActivTrades MT5 adapter (C5).

InstrumentProvider, DataClient, ExecutionClient for NautilusTrader 1.231.0
over the `MT5ClientProtocol` (FakeMT5Client / FakeMT5Broker in tests).

Ownership rule: Nautilus is authoritative for orders, fills, positions,
portfolio and PnL. This package only TRANSLATES, DEDUPLICATES, REPORTS and
RECONCILES broker truth. It keeps exactly two durable tables of its own
(`state.Mt5StateStore`): the ClientOrderId <-> MT5 ticket mapping and the set
of already-ingested MT5 deal tickets. Neither is an order, position or PnL
store.
"""
