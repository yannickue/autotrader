# ruff: noqa: E501
"""Parametric, causal intraday edge FAMILIES (ORB, GAP, OVERNIGHT, VOLREV, ROUND, LEADLAG, EOD).  Research only.

Each family maps a small frozen ``FamilySpec`` (+ frozen Train-fitted numbers) to ``CandidateArrays`` for the V1
``simulate_fast`` path.  The search-side modules never touch later-fold data; one separate sealed module is the single
accessor (contract: ``tests/test_v2_families_seal.py``).
"""
