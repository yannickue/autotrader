"""Thin mappers/adapters around NautilusTrader 1.231.0 (C4).

Nautilus owns orders, fills, positions, portfolio and PnL. Code in this
package only maps data/instruments INTO Nautilus, bridges pure risk/sizing
decisions, and reads Nautilus results back OUT. It must never keep a second
authoritative copy of order/position/portfolio state.
"""
