"""Tests for the single-owner MT5 connection lock (`lock.py`)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from adapters.activtrades_mt5.lock import acquire_mt5_lock


def test_acquire_creates_lock_file_with_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    lock = acquire_mt5_lock(lock_path)
    assert lock is not None
    try:
        assert lock_path.is_file()
        assert lock_path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_while_held_returns_none(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    first = acquire_mt5_lock(lock_path)
    assert first is not None
    try:
        second = acquire_mt5_lock(lock_path)
        assert second is None
    finally:
        first.release()


def test_release_allows_reacquisition(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    first = acquire_mt5_lock(lock_path)
    assert first is not None
    first.release()
    assert not lock_path.exists()

    second = acquire_mt5_lock(lock_path)
    assert second is not None
    second.release()


def test_stale_lock_is_cleared_and_reacquired(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    lock_path.write_text("999999999")  # a PID that isn't this test's holder
    stale_time = time.time() - 10_000  # far beyond _STALE_AFTER_SECONDS
    os.utime(lock_path, (stale_time, stale_time))

    lock = acquire_mt5_lock(lock_path)
    assert lock is not None, "a lock file far older than the staleness window must be reclaimed"
    lock.release()


def test_fresh_lock_is_not_cleared(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    held = acquire_mt5_lock(lock_path)
    assert held is not None
    try:
        again = acquire_mt5_lock(lock_path)
        assert again is None, "a freshly-held lock must never be treated as stale"
    finally:
        held.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    lock = acquire_mt5_lock(lock_path)
    assert lock is not None
    lock.release()
    lock.release()  # must not raise
