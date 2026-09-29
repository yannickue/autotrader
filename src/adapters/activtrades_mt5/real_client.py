"""The one place in this repository allowed to `import MetaTrader5` directly.

`MetaTrader5` ships as a set of module-level functions (`initialize`,
`shutdown`, `account_info`, ...), not a class -- the module object itself
already structurally satisfies `MT5ClientProtocol` (see `client.py`), so no
wrapper class is needed. Every other module, including the `scripts/mt5_*.py`
entry points, must obtain a client via `get_real_client()` rather than
importing `MetaTrader5` itself, so this file stays the single audit point for
"does this process link against the real MT5 package."
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapters.activtrades_mt5.client import MT5ClientProtocol


def get_real_client() -> MT5ClientProtocol:
    """Return the real `MetaTrader5` module as an `MT5ClientProtocol`.

    Importing `MetaTrader5` at call time (not module import time) means a
    process that never calls this function never pays the cost of loading
    the package, and a test suite that never calls it never needs the
    package importable at all.
    """
    import MetaTrader5

    return MetaTrader5
