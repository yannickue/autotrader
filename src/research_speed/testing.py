# ruff: noqa: E501
"""Picklable top-level helpers for the research_speed tests (process-pool workers must be importable by the child process)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


def square(x: int) -> int:
    return x * x


def write_marker(arg: dict[str, Any]) -> dict[str, Any]:
    """Segment-like task: writes ``<dir>/<id>.txt`` and returns a result; ``arg['sleep']`` simulates work, ``arg['fail']`` raises."""
    if arg.get("fail"):
        raise RuntimeError(f"boom {arg['id']}")
    time.sleep(arg.get("sleep", 0))
    name = str(arg["id"]).replace("/", "__")
    Path(arg["dir"], f"{name}.txt").write_text(
        f"{arg['id']}:{arg.get('value', 0) * 2}", encoding="utf-8"
    )
    return {
        "status": "BUILT",
        "id": arg["id"],
        "value": arg.get("value", 0) * 2,
        "pid": os.getpid(),
    }


def set_env(name: str, value: str) -> None:
    os.environ[name] = value


def read_env(arg: dict[str, Any]) -> dict[str, Any]:
    return {"status": "BUILT", "value": os.environ.get(arg["name"])}
