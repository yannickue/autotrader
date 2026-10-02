# ruff: noqa: E501
"""A moved-aside valid publication is never stranded or destroyed; stale cleanup restores orphaned valid copies;
the benchmark script refuses unsafe --out paths."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from research_workbench import dag
from tests.unit.research_workbench.test_workbench_dag_transaction import (
    KEY,
    _corrupt,
    _leftovers,
    _publish,
    _snapshot,
)


def _setup(tmp_path):
    donor = dag.ArtifactStore(tmp_path / "donor")
    _publish(donor)
    valid_dir = donor.stage_dir("M", "SIGNALS", KEY)
    store = dag.ArtifactStore(tmp_path / "root")
    _publish(store)
    _corrupt(store)  # invalid target => a publish tries to replace it
    return store, store.stage_dir("M", "SIGNALS", KEY), valid_dir


def _inject_valid_at_move(monkeypatch, valid_dir):
    def concurrent_valid(self, target):
        shutil.rmtree(target)
        shutil.copytree(valid_dir, target)

    monkeypatch.setattr(dag.ArtifactStore, "_before_move", concurrent_valid)


def test_interrupt_between_move_aside_and_restore_restores_the_valid_aside(
    tmp_path, monkeypatch
) -> None:
    store, final, valid_dir = _setup(tmp_path)
    _inject_valid_at_move(monkeypatch, valid_dir)

    def interrupt(self, target, aside):
        assert aside.exists() and not target.exists()  # the valid copy is aside right now
        raise KeyboardInterrupt

    monkeypatch.setattr(dag.ArtifactStore, "_after_move_aside", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _publish(store)
    monkeypatch.undo()
    assert store.lookup("M", "SIGNALS", KEY).hit  # restored by the finally block
    assert _snapshot(final) == _snapshot(valid_dir)
    assert not [n for n in _leftovers(store)]


def test_target_reappearing_invalid_while_the_aside_is_valid_keeps_the_valid_copy(
    tmp_path, monkeypatch
) -> None:
    store, final, valid_dir = _setup(tmp_path)
    _inject_valid_at_move(monkeypatch, valid_dir)

    def invalid_reappears(self, target, aside):
        target.mkdir()
        (target / "junk").write_text("not a publication", encoding="utf-8")

    monkeypatch.setattr(dag.ArtifactStore, "_after_move_aside", invalid_reappears)
    manifest = _publish(store)
    monkeypatch.undo()
    assert manifest["complete"] is True
    assert store.lookup(
        "M", "SIGNALS", KEY
    ).hit  # the valid copy won over the invalid reappeared target
    assert _snapshot(final) == _snapshot(valid_dir)
    assert not _leftovers(store) and not [p for p in final.parent.iterdir() if ".trash." in p.name]


def test_cleanup_restores_a_valid_orphan_aside_whose_target_is_missing(tmp_path) -> None:
    donor = dag.ArtifactStore(tmp_path / "donor")
    _publish(donor)
    valid_dir = donor.stage_dir("M", "SIGNALS", KEY)
    store = dag.ArtifactStore(tmp_path / "root")
    parent = store.stage_dir("M", "SIGNALS", KEY).parent
    parent.mkdir(parents=True)
    orphan = parent / f"{KEY[:40]}.stale.crashed"
    shutil.copytree(valid_dir, orphan)  # a crash stranded the only valid copy
    assert not store.lookup("M", "SIGNALS", KEY).hit
    result = dag.ArtifactStore._cleanup_leftovers(parent, KEY[:40])
    assert result["restored"] == [orphan.name] and result["removed"] == []
    assert store.lookup("M", "SIGNALS", KEY).hit and not orphan.exists()


def test_cleanup_restores_a_valid_aside_over_an_invalid_target_and_drops_redundant_ones(
    tmp_path,
) -> None:
    store, final, valid_dir = _setup(tmp_path)  # target invalid
    parent = final.parent
    shutil.copytree(valid_dir, parent / f"{KEY[:40]}.stale.a")
    result = dag.ArtifactStore._cleanup_leftovers(parent, KEY[:40])
    assert result["restored"] and store.lookup("M", "SIGNALS", KEY).hit
    shutil.copytree(
        valid_dir, parent / f"{KEY[:40]}.stale.b"
    )  # target valid now: this copy is redundant
    result = dag.ArtifactStore._cleanup_leftovers(parent, KEY[:40])
    assert store.lookup("M", "SIGNALS", KEY).hit
    assert not (parent / f"{KEY[:40]}.stale.b").exists()


# ---- benchmark script output guard ----
_BENCH = Path(__file__).resolve().parents[3] / "scripts" / "bench_workbench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_workbench_mod", _BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_out_must_be_below_the_allowed_roots(bench, tmp_path) -> None:
    with pytest.raises(SystemExit):
        bench.resolve_out_dir(str(Path.home() / "Desktop" / "x"))
    with pytest.raises(SystemExit):
        bench.resolve_out_dir(str(bench.ROOT))
    assert bench.resolve_out_dir(str(tmp_path / "ok")) == (tmp_path / "ok").resolve()
    assert bench.resolve_out_dir(str(bench.ROOT / "artifacts" / "research_workbench_bench" / "x"))


def test_bench_refuses_to_delete_an_unmarked_runs_dir(bench, tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    precious = runs / "precious.txt"
    precious.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["bench", "--out", str(tmp_path)])
    assert bench.main() == 2
    assert precious.read_text(encoding="utf-8") == "keep"
