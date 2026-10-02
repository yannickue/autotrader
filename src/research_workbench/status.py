# ruff: noqa: E501
"""Promotion status of a research experiment (OFFLINE ONLY).

There is deliberately NO live / READY_FOR_LIVE / PROMOTE_TO_LIVE member: nothing the workbench produces may be
promoted to live trading by a status value. ``READY_FOR_SHADOW_RESEARCH`` is the highest status and still means
"research only".
"""

from __future__ import annotations

from enum import StrEnum


class PromotionStatus(StrEnum):
    REJECT_FAST = "REJECT_FAST"
    PROMOTE_TO_FIDELITY = "PROMOTE_TO_FIDELITY"
    FIDELITY_MISMATCH = "FIDELITY_MISMATCH"
    READY_FOR_ROBUSTNESS = "READY_FOR_ROBUSTNESS"
    ROBUSTNESS_FAILED = "ROBUSTNESS_FAILED"
    READY_FOR_SHADOW_RESEARCH = "READY_FOR_SHADOW_RESEARCH"


__all__ = ("PromotionStatus",)
