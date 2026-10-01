# ruff: noqa: E501
"""Deployment-approval gate of the NORMAL daily task (Lane R addendum).

The normal task (``AutoTrader-DemoDaily`` -> ``run_trader_day.ps1`` -> ``supervisor.py``) must never launch an unfinished or
unapproved system.  It refuses to start the runner unless ``<artifacts>/deploy_approved.json`` exists with

    {"sha": "<full git sha>", "approved_utc": "<iso>"}

AND that sha equals ``git rev-parse HEAD`` of the checkout the script lives in AND ``git status --porcelain`` (tracked files only)
of that checkout is clean.  Verdicts (alert reason + supervisor exit code 30, ``SUP_NOT_APPROVED``, NEVER restarted):

* ``NOT_APPROVED``     the file is missing / unreadable / carries no sha,
* ``SHA_MISMATCH``     the approved sha is not the checked-out HEAD (stale approval or new commits),
* ``DIRTY_CHECKOUT``   tracked files are modified (a modified checkout is not what was approved),
* ``GIT_UNAVAILABLE``  git could not be asked (fail closed).

The lead writes the file only at the final controlled deployment (``approve_deploy.py`` / ``approve_deploy.ps1``; ``--revoke`` /
``-Revoke`` removes it).  The EOD-recovery path (``eod_recovery.py`` -> ``demo_trader.py --flatten-only``) is deliberately NOT gated
by this file nor by STOP: flattening own exposure must always be possible.  Pure stdlib; git is injectable for tests.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

APPROVAL_FILE = "deploy_approved.json"
EXIT_NOT_APPROVED = 30  # supervisor exit code: deployment gate refused (not restartable)

OK = "OK"
NOT_APPROVED = "NOT_APPROVED"
SHA_MISMATCH = "SHA_MISMATCH"
DIRTY_CHECKOUT = "DIRTY_CHECKOUT"
GIT_UNAVAILABLE = "GIT_UNAVAILABLE"

GitFn = Callable[[Path, Sequence[str]], str]


def run_git(repo_root: Path, args: Sequence[str]) -> str:
    out = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=60, check=True)
    return out.stdout


@dataclass(frozen=True)
class GateResult:
    verdict: str
    detail: str
    head: str | None = None
    approved_sha: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == OK


def read_approval(artifacts: Path) -> dict | None:
    try:
        data = json.loads((artifacts / APPROVAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_deploy_approval(artifacts: Path, repo_root: Path = REPO_ROOT, git: GitFn | None = None) -> GateResult:
    git = git or run_git  # resolved at call time (tests patch ``run_git``)
    approval = read_approval(artifacts)
    sha = approval.get("sha") if approval else None
    if not isinstance(sha, str) or len(sha.strip()) < 40:
        return GateResult(NOT_APPROVED, f"{artifacts / APPROVAL_FILE} is missing / unreadable / has no full git sha: the lead has not "
                          "approved a deployment (approve_deploy.py)")
    sha = sha.strip().lower()
    try:
        head = git(repo_root, ["rev-parse", "HEAD"]).strip().lower()
        dirty = git(repo_root, ["status", "--porcelain", "--untracked-files=no"]).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return GateResult(GIT_UNAVAILABLE, f"cannot verify the checkout with git ({type(exc).__name__}): failing closed", approved_sha=sha)
    if head != sha:
        return GateResult(SHA_MISMATCH, f"approved sha {sha[:12]} != checked-out HEAD {head[:12]}: stale approval / new commits",
                          head=head, approved_sha=sha)
    if dirty:
        first = dirty.splitlines()[0]
        return GateResult(DIRTY_CHECKOUT, f"tracked files are modified ({len(dirty.splitlines())} path(s), e.g. {first!r}): the checkout "
                          "is not what was approved", head=head, approved_sha=sha)
    return GateResult(OK, f"deployment approved for {head[:12]} (clean checkout)", head=head, approved_sha=sha)


def write_approval(artifacts: Path, repo_root: Path = REPO_ROOT, git: GitFn | None = None, *, now: datetime | None = None) -> GateResult:
    """Approve the CURRENT head.  Refuses a dirty checkout (it could never pass the gate)."""
    git = git or run_git
    head = git(repo_root, ["rev-parse", "HEAD"]).strip().lower()
    dirty = git(repo_root, ["status", "--porcelain", "--untracked-files=no"]).strip()
    if dirty:
        return GateResult(DIRTY_CHECKOUT, "refusing to approve: tracked files are modified", head=head)
    artifacts.mkdir(parents=True, exist_ok=True)
    body = {"sha": head, "approved_utc": (now or datetime.now(UTC)).isoformat(timespec="seconds")}
    tmp = artifacts / (APPROVAL_FILE + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
    os.replace(tmp, artifacts / APPROVAL_FILE)
    return GateResult(OK, f"approved {head[:12]}", head=head, approved_sha=head)


def revoke_approval(artifacts: Path) -> bool:
    path = artifacts / APPROVAL_FILE
    existed = path.exists()
    with contextlib.suppress(OSError):
        path.unlink()
    return existed


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Deployment-approval gate: --check (default), --approve (lead only) or --revoke")
    p.add_argument("--artifacts", type=Path, default=REPO_ROOT / "artifacts" / "demo_100k")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--approve", action="store_true", help="write deploy_approved.json for the CURRENT HEAD (refused if dirty)")
    mode.add_argument("--revoke", action="store_true", help="delete deploy_approved.json")
    a = p.parse_args(argv)
    if a.revoke:
        print("revoked" if revoke_approval(a.artifacts) else "nothing to revoke (no approval file)")
        return 0
    res = write_approval(a.artifacts) if a.approve else check_deploy_approval(a.artifacts)
    print(json.dumps({"verdict": res.verdict, "detail": res.detail, "head": res.head, "approved_sha": res.approved_sha}))
    return 0 if res.ok else EXIT_NOT_APPROVED


if __name__ == "__main__":
    sys.exit(main())
