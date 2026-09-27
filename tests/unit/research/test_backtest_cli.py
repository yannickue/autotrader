import json
import subprocess
import sys
from pathlib import Path

from scripts.backtest import main

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_backtest_cli_prints_compact_machine_readable_summary(tmp_path, capsys) -> None:
    trades = tmp_path / "trades.jsonl"
    trades.write_text(
        '{"gross_pnl":"10","notional":"100","fees":"1"}\n'
        '{"gross_pnl":"-2","notional":"50","slippage":"1"}\n',
        encoding="utf-8",
    )

    exit_code = main(
        ["--trades", str(trades), "--initial-equity", "100", "--split", "oos"]
    )

    output = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert "\n" not in output
    assert json.loads(output)["metrics"]["net_pnl"] == "6"
    assert json.loads(output)["split"] == "oos"


def test_research_scripts_are_directly_runnable() -> None:
    for script in ("backtest.py", "replay.py"):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / script), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
