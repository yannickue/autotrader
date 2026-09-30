#!/usr/bin/env bash
# Thin wrapper: scripts/test.sh fast|integration|safety|slow|full|changed [args]
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/run_tests.py "$@"
