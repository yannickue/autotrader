"""The MT5 execution lane: ONE dedicated thread through which ALL MT5 IPC flows.

Why: the MetaTrader5 API is blocking and not safe for concurrent use against one terminal.
Running it on the asyncio event loop stalls the whole node; running it from several threads
races. This module gives:

- a single worker thread (`Mt5Executor`), FIFO, so MT5 calls never overlap;
- `run()` (awaitable) / `run_sync()` entry points with an OPTIONAL timeout;
- explicit per-symbol critical sections (`symbol_section`) so execution-changing operations on one
  symbol (submit / modify / cancel / protect / reduce / reconcile-sensitive work) are strictly
  ordered even if the lane were ever widened;
- a guard (`assert_in_lane`) that the session uses to REFUSE any MT5 call from another thread.

TIMEOUT SEMANTICS (safety-critical): a lane timeout means "the caller stopped waiting", NOT "the
call failed". The worker keeps running, a blocked C call cannot be cancelled, and an `order_send`
may already have reached the broker. `LaneTimeout` therefore carries no verdict; callers of
exposure-changing operations must treat it as OUTCOME UNKNOWN (reconcile from broker history) and
must NEVER resend.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any


class LaneTimeout(TimeoutError):
    """The caller stopped waiting. The MT5 call may still be running or may have completed."""


class LaneViolation(RuntimeError):
    """An MT5 call was attempted outside the MT5 execution lane."""


class LaneClosed(RuntimeError):
    pass


@dataclass(slots=True)
class LaneStats:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    timeouts: int = 0
    max_concurrent: int = 0  # must stay 1
    _active: int = 0
    worker_thread_ids: set[int] = field(default_factory=set)


class Mt5Executor:
    def __init__(self, name: str = "mt5-lane") -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._guard = threading.Lock()
        self._symbol_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._closed = False
        self.stats = LaneStats()
        self.name = name
        self._thread_id: int | None = None
        # Learn the worker's thread id deterministically (single worker).
        self._thread_id = self._pool.submit(threading.get_ident).result()

    # -- guard ----------------------------------------------------------------------

    @property
    def in_lane(self) -> bool:
        return threading.get_ident() == self._thread_id

    def assert_in_lane(self, what: str = "MT5 call") -> None:
        if not self.in_lane:
            raise LaneViolation(f"{what} attempted outside the MT5 execution lane")

    # -- execution ------------------------------------------------------------------

    def _wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def runner(*args: Any, **kwargs: Any) -> Any:
            with self._guard:
                self.stats._active += 1
                self.stats.max_concurrent = max(self.stats.max_concurrent, self.stats._active)
                self.stats.worker_thread_ids.add(threading.get_ident())
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                with self._guard:
                    self.stats.failed += 1
                raise
            else:
                with self._guard:
                    self.stats.completed += 1
                return result
            finally:
                with self._guard:
                    self.stats._active -= 1

        return runner

    def submit(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future:
        if self._closed:
            raise LaneClosed(self.name)
        with self._guard:
            self.stats.submitted += 1
        if self.in_lane:  # re-entrant use from inside the lane runs inline (no self-deadlock)
            future: concurrent.futures.Future = concurrent.futures.Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        return self._pool.submit(self._wrap(fn), *args, **kwargs)

    def run_sync(
        self, fn: Callable[..., Any], *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        future = self.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            with self._guard:
                self.stats.timeouts += 1
            raise LaneTimeout(f"{getattr(fn, '__name__', fn)} exceeded {timeout}s") from None

    async def run(
        self, fn: Callable[..., Any], *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        future = self.submit(fn, *args, **kwargs)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except TimeoutError:
            with self._guard:
                self.stats.timeouts += 1
            raise LaneTimeout(f"{getattr(fn, '__name__', fn)} exceeded {timeout}s") from None

    # -- per-symbol serialization ----------------------------------------------------------

    @contextlib.contextmanager
    def symbol_section(self, symbol: str) -> Iterator[None]:
        """Execution-changing work on `symbol` is strictly ordered (re-entrant in the lane)."""
        lock = self._symbol_locks[symbol]
        with lock:
            yield

    def shutdown(self, wait: bool = True) -> None:
        self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)


class LoopBridge:
    """Runs a callable on the event-loop thread and waits for its result.

    Nautilus objects (message bus, cache, orders) are only ever touched on the loop thread. Code
    running in the MT5 lane reaches them through this bridge. On the loop thread it is a direct
    call, so single-threaded tests behave exactly as before."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread_id: int | None = None
        self.calls_marshalled = 0

    def bind_current_thread(self) -> None:
        self._thread_id = threading.get_ident()

    @property
    def on_loop_thread(self) -> bool:
        return self._thread_id is None or threading.get_ident() == self._thread_id

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.on_loop_thread:
            return fn(*args, **kwargs)
        self.calls_marshalled += 1
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)

        self._loop.call_soon_threadsafe(run)
        return future.result()
