# ruff: noqa: E501
"""OFFLINE, READ-ONLY retrospective coverage analysis (hindsight diagnostics; never a trading input).

Market-first question: for every directional market move, was it EXECUTED, REJECTED, a NEAR_MISS,
OUT_OF_WINDOW, or NO_SETUP -- and is "this looks like missed alpha" distinguishable from what random bars
look like (false-positive control)?  Nothing here is imported by the live engine, writes to a store, or
influences a gate.
"""

HINDSIGHT_LABEL = "HINDSIGHT DIAGNOSTICS: moves are found with future bars; never a gate, filter or model input"
