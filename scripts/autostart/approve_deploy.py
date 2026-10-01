# ruff: noqa: E501
"""Lead-only: write ``<artifacts>/deploy_approved.json`` for the CURRENT git HEAD (``--revoke`` removes it).

Thin entry point over ``deploy_gate`` (``approve_deploy.py`` == ``deploy_gate.py --approve``); see that module for the contract."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import deploy_gate


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--revoke" not in args and "--check" not in args and "--approve" not in args:
        args.append("--approve")
    return deploy_gate.main(args)


if __name__ == "__main__":
    sys.exit(main())
