"""C4: run the real-Nautilus GER40 lifecycle proof from a validated C3 dataset.

No MT5 access at all (pure offline). Usage:
    <PY> scripts/c4_run_proof.py [DATASET_PARQUET] [OUT_DIR]
Defaults: newest data/c3_sample/.../bars_5m/*.parquet, artifacts/c4.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from nautilus_kernel.proof import evidence_document, run_proof  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        dataset = Path(argv[0])
    else:
        candidates = sorted(
            (REPO_ROOT / "data" / "c3_sample").glob("ACTIVTRADES_MT5_CFD/GER40/bars_5m/*.parquet")
        )
        if not candidates:
            print("no C3 dataset found; run scripts/mt5_download_sample.py first", file=sys.stderr)
            return 2
        dataset = candidates[-1]
    out = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "artifacts" / "c4"
    out.mkdir(parents=True, exist_ok=True)
    symbol_info = REPO_ROOT / "tests" / "fixtures" / "ger40" / "symbol_info.json"
    run = run_proof(dataset_path=dataset, symbol_info_path=symbol_info, work_dir=out)
    doc = evidence_document(run)
    (out / "evidence.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    print(json.dumps(run.result.metrics, indent=1))
    print("evidence:", out / "evidence.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
