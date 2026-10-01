# ruff: noqa: E501
"""Research-pipeline speed infrastructure (OFFLINE / RESEARCH ONLY; development tooling).

Nothing in here is imported by the live trader (src/demo, src/exits, src/execution, src/risk ...) and nothing in here
changes a computed number: it only decides WHICH independent unit of work runs, in WHICH process, and whether a
previous result with an identical fingerprint can be reused.

* ``parallel``     - bounded, deterministic process fan-out (max 3 workers, memory guard)
* ``importgraph``  - static import closure -> content hash of exactly the code a result depends on
* ``artifact``     - the six-component artifact fingerprint -> ARTIFACT_ID
* ``segments``     - per-segment atomic manifests (checkpoint / resume / cache hit with explicit invalidation reasons)
* ``progress``     - status file with heartbeat and wall-clock per segment
* ``scheduler``    - dependency-aware (DAG) scheduler over the segments
"""

from __future__ import annotations

__all__ = ["artifact", "importgraph", "parallel", "progress", "scheduler", "segments"]
