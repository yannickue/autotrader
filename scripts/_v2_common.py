"""Shared helpers for the V2 market scripts (attach-only, read-only, no credentials in files)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def find_env_file() -> Path | None:
    """Locate the git-ignored `.env` WITHOUT reading it.

    Order: `MT5_ENV_FILE` env var, `<repo>/.env`, then the `.env` of the main working tree
    (this repo may be a git worktree that has no `.env` of its own).
    """
    explicit = os.environ.get("MT5_ENV_FILE")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    local = REPO_ROOT / ".env"
    if local.is_file():
        return local
    try:
        common = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if common:
            main_env = Path(common) if Path(common).is_absolute() else REPO_ROOT / common
            main_env = main_env.resolve().parent / ".env"
            if main_env.is_file():
                return main_env
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def load_config():
    """Attach-only MT5 config (refuses when account login is opted in)."""
    from adapters.config import load_attach_only_config

    return load_attach_only_config(env_file=find_env_file())
