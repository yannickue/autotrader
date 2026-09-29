"""Shared plumbing for Group B candidate strategies (label lookup + candidate construction)."""

from __future__ import annotations

from alpha.strategies._shared import GroupCStrategyBase


class GroupBStrategyBase(GroupCStrategyBase):
    """Group B reuses the causal label lookup and uniform candidate construction."""
