# ruff: noqa: E501
"""Dependency-aware scheduler over independent segments.

``jobs == 1`` runs the tasks in-process in list order (respecting dependencies); ``jobs > 1`` fans out over a process pool of
at most ``clamp_jobs`` workers. A task is submitted when all of its dependencies are done; the submission order among ready
tasks is the list order. Because tasks exchange data only through files below their own output paths, and every result is
stored under its task id, the outcome does not depend on the number of workers or on completion order.

A failing task never aborts the others: it is recorded, its dependants are marked ``skipped_dep_failed``.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from research_speed.parallel import clamp_jobs
from research_speed.progress import CACHED, DONE, FAILED, SKIPPED, StatusFile


@dataclass(frozen=True)
class Task:
    id: str
    fn: Callable[[Any], dict[str, Any]]  # top-level (picklable) function
    arg: Any
    deps: tuple[str, ...] = ()


def _state_of(result: dict[str, Any]) -> str:
    return CACHED if result.get("status") == "CACHE_HIT" else DONE


def _validate(tasks: list[Task]) -> None:
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task ids")
    known = set(ids)
    for t in tasks:
        missing = [d for d in t.deps if d not in known]
        if missing:
            raise ValueError(f"task {t.id} depends on unknown task(s) {missing}")


def run_dag(
    tasks: list[Task],
    jobs: int,
    status: StatusFile | None = None,
    *,
    initializer: Callable[..., None] | None = None,
    initargs: tuple[Any, ...] = (),
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Returns ``(results, errors)`` keyed by task id. ``errors`` holds the traceback text of failed / the reason of skipped tasks.

    ``initializer(*initargs)`` runs once in every worker process BEFORE its first task is unpickled (use it to fix ``sys.path``)."""
    _validate(tasks)
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    pending = list(tasks)
    n = clamp_jobs(jobs, len(tasks)) if jobs > 1 else 1

    def blocked(t: Task) -> bool:
        return any(d in errors for d in t.deps)

    def ready(t: Task) -> bool:
        return all(d in results for d in t.deps)

    def settle_skips() -> None:
        changed = True
        while changed:
            changed = False
            for t in list(pending):
                if blocked(t):
                    pending.remove(t)
                    errors[t.id] = "SKIPPED: a dependency failed"
                    if status:
                        status.finish(t.id, SKIPPED, wall_s=0.0)
                    changed = True

    if n == 1:
        while pending:
            settle_skips()
            nxt = next((t for t in pending if ready(t)), None)
            if nxt is None:
                break
            pending.remove(nxt)
            if status:
                status.start(nxt.id)
            try:
                res = nxt.fn(nxt.arg)
                results[nxt.id] = res
                if status:
                    status.finish(nxt.id, _state_of(res))
            except Exception:
                errors[nxt.id] = traceback.format_exc()
                if status:
                    status.finish(nxt.id, FAILED, error=errors[nxt.id].splitlines()[-1])
        return results, errors

    running: dict[Future, Task] = {}
    with ProcessPoolExecutor(max_workers=n, initializer=initializer, initargs=initargs) as ex:
        while pending or running:
            settle_skips()
            for t in list(pending):
                if len(running) >= n:
                    break
                if ready(t):
                    pending.remove(t)
                    if status:
                        status.start(t.id)
                    running[ex.submit(t.fn, t.arg)] = t
            if not running:
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for f in done:
                t = running.pop(f)
                try:
                    res = f.result()
                    results[t.id] = res
                    if status:
                        status.finish(t.id, _state_of(res))
                except Exception:
                    errors[t.id] = traceback.format_exc()
                    if status:
                        status.finish(t.id, FAILED, error=errors[t.id].splitlines()[-1])
    for t in pending:  # deadlock guard (cannot happen with a validated acyclic graph)
        errors.setdefault(t.id, "NOT RUN: unsatisfied dependencies")
    return results, errors
