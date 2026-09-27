"""Small helpers for immutable JSON-compatible research metadata."""

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any


def freeze(value: Any) -> Any:
    """Defensively copy and recursively freeze JSON-like values."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return deepcopy(value)


def thaw(value: Any) -> Any:
    """Return a serialization-friendly defensive copy of a frozen value."""

    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [thaw(item) for item in value]
    return deepcopy(value)
