import json
from datetime import UTC, date, datetime

from research.storage import ParquetStore, PartitionKey
from scripts.replay import main


def test_replay_cli_reports_dataset_and_configuration_hashes(tmp_path, capsys) -> None:
    key = PartitionKey("venue-a", "BTCUSDT-PERP", "quote", date(2026, 1, 1), "v1")
    ParquetStore(tmp_path).write_partition(
        key,
        (
            {
                "source_key": "event-1",
                "event_time": datetime(2026, 1, 1, tzinfo=UTC),
                "available_at": datetime(2026, 1, 1, tzinfo=UTC),
                "bid": 100.0,
            },
        ),
        schema_version=1,
    )

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "--venue",
            "venue-a",
            "--instrument",
            "BTCUSDT-PERP",
            "--event-type",
            "quote",
            "--date",
            "2026-01-01",
            "--dataset-version",
            "v1",
            "--configuration",
            '{"latency_model":"recorded"}',
        ]
    )

    output = capsys.readouterr().out.strip()
    payload = json.loads(output)
    assert exit_code == 0
    assert "\n" not in output
    assert payload["event_count"] == 1
    assert len(payload["dataset_hash"]) == 64
    assert len(payload["configuration_hash"]) == 64
