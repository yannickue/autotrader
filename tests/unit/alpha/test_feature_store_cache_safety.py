"""FeatureStore cache safety: atomic publish, verified load (defect = MISS), code fingerprint."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import alpha.fast.store as store_module
from alpha.fast.store import FeatureConfig, FeatureStore
from research_speed import importgraph

CONFIG = FeatureConfig()


def _bars(periods: int = 300) -> pd.DataFrame:
    ts = pd.date_range("2024-03-28 00:00", periods=periods, freq="5min", tz="UTC")
    x = np.arange(periods, dtype=float)
    close = 100.0 + 0.02 * x + np.sin(x / 9.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close - 0.1,
            "high": close + 0.7 + (x % 7) / 100,
            "low": close - 0.6 - (x % 5) / 100,
            "close": close,
            "spread_pts": np.full(periods, 1.5),
        }
    )


def _same(left, right) -> bool:
    assert sorted(left) == sorted(right)
    return all(
        left[n].dtype == right[n].dtype and np.array_equal(left[n], right[n], equal_nan=True)
        for n in left
    )


@pytest.fixture
def frame() -> pd.DataFrame:
    return _bars()


@pytest.fixture
def fresh(frame):
    return FeatureStore.build(frame, CONFIG)


def _dirs(cache: Path) -> list[Path]:
    return [p for p in cache.iterdir() if p.is_dir()]


def _published(tmp_path: Path, frame) -> tuple[Path, Path, Path]:
    FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    (target,) = _dirs(tmp_path)
    return target, target / "manifest.json", target / "features.npz"


def _rebuilds_equal_fresh(tmp_path, frame, fresh) -> None:
    again = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert again.metadata["cache_hit"] is False
    assert _same(again, fresh)
    third = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert third.metadata["cache_hit"] is True
    assert _same(third, fresh)


def test_cold_equals_warm_bit_identical_and_manifest_fields(tmp_path, frame, fresh) -> None:
    cold = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    warm = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert cold.metadata["cache_hit"] is False and warm.metadata["cache_hit"] is True
    assert _same(cold, warm) and _same(warm, fresh)
    (target,) = _dirs(tmp_path)
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["cache_format_version"] == store_module.CACHE_FORMAT_VERSION
    assert manifest["artifact"]["size_bytes"] == (target / "features.npz").stat().st_size
    assert manifest["artifact"]["sha256"] == store_module._sha256_file(target / "features.npz")
    assert set(manifest["arrays"]) == set(manifest["array_shapes"]) == set(fresh)
    assert all(manifest["array_shapes"][n] == list(fresh[n].shape) for n in fresh)
    assert sorted(p.name for p in target.iterdir()) == ["features.npz", "manifest.json"]


def test_interrupted_npz_write_leaves_no_manifest_and_next_call_rebuilds(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    def boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(store_module.np, "savez_compressed", boom)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert "cache_write_skipped" in result.metadata and result.metadata["cache_hit"] is False
    assert _same(result, fresh)
    monkeypatch.undo()
    (target,) = _dirs(tmp_path)
    assert list(target.iterdir()) == []  # no manifest, no artifact, no temp leftovers
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_interrupted_replace_leaves_no_committed_manifest(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def replace_then_die_on_manifest(src, dst):
        calls.append(str(dst))
        if str(dst).endswith("manifest.json"):
            raise OSError("crash before commit")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", replace_then_die_on_manifest)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert "cache_write_skipped" in result.metadata and _same(result, fresh)
    monkeypatch.undo()
    (target,) = _dirs(tmp_path)
    assert calls[0].endswith("features.npz")  # arrays were published first ...
    assert not (target / "manifest.json").exists()  # ... but are not committed
    assert [p.name for p in target.iterdir()] == ["features.npz"]  # temp manifest cleaned
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_interrupted_rewrite_never_leaves_valid_manifest_over_bad_arrays(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    target, manifest_path, _ = _published(tmp_path, frame)
    (target / "features.npz").write_bytes(b"corrupt")  # force a miss -> rewrite

    def boom(*args, **kwargs):
        raise OSError("interrupted")

    monkeypatch.setattr(store_module.np, "savez_compressed", boom)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert "cache_write_skipped" in result.metadata and _same(result, fresh)
    monkeypatch.undo()
    assert not manifest_path.exists()
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_corrupt_npz_is_miss_and_rebuild_equals_fresh(tmp_path, frame, fresh) -> None:
    _, _, npz = _published(tmp_path, frame)
    npz.write_bytes(npz.read_bytes()[:-50] + b"X" * 50)
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_truncated_npz_is_miss(tmp_path, frame, fresh) -> None:
    _, _, npz = _published(tmp_path, frame)
    npz.write_bytes(npz.read_bytes()[: npz.stat().st_size // 2])
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_missing_npz_is_miss(tmp_path, frame, fresh) -> None:
    _, _, npz = _published(tmp_path, frame)
    npz.unlink()
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_corrupt_manifest_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    manifest.write_text("{not json at all", encoding="utf-8")
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_truncated_manifest_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text[: len(text) // 2], encoding="utf-8")
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_empty_or_non_dict_manifest_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    manifest.write_text("[]", encoding="utf-8")
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def _edit_manifest(manifest: Path, mutate) -> None:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    mutate(data)
    manifest.write_text(json.dumps(data), encoding="utf-8")


def test_wrong_sha256_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    _edit_manifest(manifest, lambda d: d["artifact"].update(sha256="0" * 64))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_wrong_size_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    _edit_manifest(manifest, lambda d: d["artifact"].update(size_bytes=1))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_missing_array_in_npz_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, npz = _published(tmp_path, frame)
    partial = {n: fresh[n] for n in list(fresh)[:-1]}
    with npz.open("wb") as handle:
        np.savez_compressed(handle, **partial)
    # keep size/sha consistent so only the declared-array check can catch it
    _edit_manifest(
        manifest,
        lambda d: d["artifact"].update(
            sha256=store_module._sha256_file(npz), size_bytes=npz.stat().st_size
        ),
    )
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_wrong_declared_dtype_or_shape_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    name = next(iter(fresh))
    _edit_manifest(manifest, lambda d: d["array_shapes"].update({name: [1]}))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)
    _edit_manifest(manifest, lambda d: d["arrays"].update({name: "float16"}))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_key_and_key_components_mismatch_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    _edit_manifest(manifest, lambda d: d.update(key="deadbeef"))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)
    _edit_manifest(manifest, lambda d: d["key_components"].update(parameters={"x": 1}))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_old_format_manifest_without_artifact_is_miss(tmp_path, frame, fresh) -> None:
    _, manifest, _ = _published(tmp_path, frame)
    _edit_manifest(manifest, lambda d: (d.pop("artifact"), d.pop("array_shapes")))
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


def test_changed_data_and_config_change_key(tmp_path, frame) -> None:
    FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    changed = frame.copy()
    changed.loc[10, "close"] += 1
    assert FeatureStore.load_or_build(changed, CONFIG, tmp_path).metadata["cache_hit"] is False
    other = replace(CONFIG, swing_order=4)
    assert FeatureStore.load_or_build(frame, other, tmp_path).metadata["cache_hit"] is False
    assert len(_dirs(tmp_path)) == 3
    assert FeatureStore.load_or_build(frame, CONFIG, tmp_path).metadata["cache_hit"] is True


def test_changed_code_fingerprint_changes_key(tmp_path, frame, monkeypatch) -> None:
    first = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    monkeypatch.setattr(store_module, "_code_fingerprint", lambda: "changed-code")
    second = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert second.metadata["cache_hit"] is False
    assert second.metadata["cache_key"] != first.metadata["cache_key"]


def _key(frame) -> str:
    return store_module._cache_key(store_module._key_components(frame, CONFIG))


def _tmp_tree(root: Path) -> Path:
    src = root / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "entry.py").write_text("from pkg import dep\nVALUE = dep.X\n")
    (src / "pkg" / "dep.py").write_text("X = 1\n")
    (src / "pkg" / "unrelated.py").write_text("Y = 2\n")
    return src


def test_fingerprint_follows_real_closure_edits_not_unrelated_file_edits(tmp_path) -> None:
    src = _tmp_tree(tmp_path)
    entries = (src / "pkg" / "entry.py",)
    baseline = store_module._code_fingerprint(entries, src)
    assert not baseline.startswith("UNCACHEABLE")
    (src / "pkg" / "unrelated.py").write_text("Y = 999  # real edit outside the closure\n")
    assert store_module._code_fingerprint(entries, src) == baseline
    (src / "pkg" / "dep.py").write_text("X = 2\n")  # real edit of a dependency
    changed = store_module._code_fingerprint(entries, src)
    assert changed != baseline
    (src / "pkg" / "dep.py").write_text("import importlib\nX = importlib.import_module('os')\n")
    assert store_module._code_fingerprint(entries, src).startswith("UNCACHEABLE:dynamic_import:")


def test_real_store_fingerprint_is_cacheable_and_covers_session(frame) -> None:
    assert not store_module._code_fingerprint().startswith("UNCACHEABLE")


def test_closure_covers_formerly_unfingerprinted_dependencies() -> None:
    src_root = Path(store_module.__file__).resolve().parents[2]
    files = {
        p.relative_to(src_root).as_posix()
        for p in importgraph.closure([Path(store_module.__file__).resolve()], [src_root])
    }
    assert {"alpha/fast/store.py", "alpha/session.py", "alpha/common/dataset.py"} <= files


def test_dynamic_import_in_closure_disables_cache(tmp_path, frame, fresh, monkeypatch) -> None:
    monkeypatch.setattr(
        importgraph,
        "dynamic_import_files",
        lambda files: [Path(store_module.__file__).resolve()],
    )
    assert store_module._code_fingerprint().startswith("UNCACHEABLE:dynamic_import:")
    first = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    second = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert first.metadata["cache_hit"] is False and second.metadata["cache_hit"] is False
    assert _same(first, fresh) and _same(second, fresh)
    assert list(tmp_path.iterdir()) == []  # nothing published, nothing read


def test_fingerprint_failure_disables_cache(tmp_path, frame, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("cannot analyse")

    monkeypatch.setattr(importgraph, "closure", broken)
    assert store_module._code_fingerprint().startswith("UNCACHEABLE:fingerprint_error")
    assert FeatureStore.load_or_build(frame, CONFIG, tmp_path).metadata["cache_hit"] is False
    assert list(tmp_path.iterdir()) == []


def test_old_cache_format_never_hits(tmp_path, frame, monkeypatch) -> None:
    FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    monkeypatch.setattr(store_module, "CACHE_FORMAT_VERSION", store_module.CACHE_FORMAT_VERSION - 1)
    assert FeatureStore.load_or_build(frame, CONFIG, tmp_path).metadata["cache_hit"] is False


def test_store_has_no_module_level_research_imports() -> None:
    text = Path(store_module.__file__).read_text(encoding="utf-8")
    top = [ln for ln in text.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("research_speed" in ln for ln in top)


def test_timezone_rules_fingerprint_is_in_key_and_changes_it(frame, monkeypatch) -> None:
    components = store_module._key_components(frame, CONFIG)
    tz = components["timezone_rules"]
    assert "Europe/Berlin" in tz["zones"] and "tzdata" in tz["packages"]
    baseline = store_module._cache_key(components)
    monkeypatch.setattr(
        store_module, "_timezone_fingerprint", lambda names: {"zones": {"x": "new-rules"}}
    )
    assert _key(frame) != baseline


def test_timezone_digest_is_deterministic_and_zone_specific() -> None:
    assert store_module._tz_rules_digest("Europe/Berlin") == store_module._tz_rules_digest(
        "Europe/Berlin"
    )
    assert store_module._tz_rules_digest("Europe/Berlin") != store_module._tz_rules_digest("UTC")


def test_session_tz_is_fingerprinted(frame) -> None:
    other = replace(CONFIG, session=replace(CONFIG.session, tz="America/New_York"))
    zones = store_module._key_components(frame, other)["timezone_rules"]["zones"]
    assert set(zones) == {"Europe/Berlin", "America/New_York"}


def test_two_writers_same_key_no_raise_valid_manifest_cold_equals_warm(
    tmp_path, frame, fresh
) -> None:
    results: list = []
    errors: list = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(FeatureStore.load_or_build(frame, CONFIG, tmp_path))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 4
    assert all(_same(r, fresh) for r in results)
    (target,) = _dirs(tmp_path)
    assert sorted(p.name for p in target.iterdir()) == ["features.npz", "manifest.json"]
    warm = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert warm.metadata["cache_hit"] is True and _same(warm, fresh)


def test_failing_writer_leaves_valid_published_entry_untouched(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    target, manifest, npz = _published(tmp_path, frame)
    before = (manifest.read_bytes(), npz.read_bytes())

    def boom(*args, **kwargs):
        raise OSError("x")

    monkeypatch.setattr(store_module, "_write_artifacts", boom)
    key = store_module._cache_key(store_module._key_components(frame, CONFIG))
    components = store_module._key_components(frame, CONFIG)
    # another writer already committed this exact entry: the loser skips, never deletes/rewrites
    assert store_module._publish(target, key, components, fresh, {}) is None
    assert (manifest.read_bytes(), npz.read_bytes()) == before
    # and even a failing write over an invalid entry never touches a manifest it did not write
    monkeypatch.setattr(store_module, "_verified_cache_hit", lambda *a, **k: None)
    assert "OSError" in store_module._publish(target, key, components, fresh, {})


def test_valid_entry_published_by_another_writer_is_not_rewritten(tmp_path, frame, fresh) -> None:
    key = store_module._cache_key(store_module._key_components(frame, CONFIG))
    components = store_module._key_components(frame, CONFIG)
    target, manifest, npz = _published(tmp_path, frame)
    stamp = (manifest.stat().st_mtime_ns, npz.stat().st_mtime_ns)
    assert store_module._publish(target, key, components, fresh, dict(fresh.metadata)) is None
    assert (manifest.stat().st_mtime_ns, npz.stat().st_mtime_ns) == stamp


def test_lock_busy_skips_cache_write_but_returns_arrays(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    monkeypatch.setattr(store_module, "_LOCK_WAIT_SECONDS", 0.2)
    key = store_module._cache_key(store_module._key_components(frame, CONFIG))
    target = tmp_path / key
    target.mkdir()
    (target / ".publish.lock").write_text("other-writer")
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert result.metadata["cache_write_skipped"] == "publish lock busy"
    assert _same(result, fresh)
    assert (target / ".publish.lock").read_text() == "other-writer"  # not ours: untouched
    assert not (target / "manifest.json").exists()


def test_stale_lock_is_taken_over(tmp_path, frame, fresh, monkeypatch) -> None:
    key = store_module._cache_key(store_module._key_components(frame, CONFIG))
    target = tmp_path / key
    target.mkdir()
    lock = target / ".publish.lock"
    lock.write_text("dead-writer")
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert "cache_write_skipped" not in result.metadata
    assert FeatureStore.load_or_build(frame, CONFIG, tmp_path).metadata["cache_hit"] is True
    assert not lock.exists()


def test_replace_retries_on_permission_error_then_succeeds(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    real_replace = os.replace
    failures = {"n": 0}

    def flaky(src, dst):
        if str(dst).endswith("features.npz") and failures["n"] < 2:
            failures["n"] += 1
            raise PermissionError("file in use")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    monkeypatch.undo()
    assert failures["n"] == 2 and "cache_write_skipped" not in result.metadata
    assert FeatureStore.load_or_build(frame, CONFIG, tmp_path).metadata["cache_hit"] is True


def test_persistent_permission_error_returns_arrays_not_raise(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    real_replace = os.replace

    def locked(src, dst):
        if str(dst).endswith("features.npz"):
            raise PermissionError("held open")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", locked)
    monkeypatch.setattr(store_module.time, "sleep", lambda s: None)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    monkeypatch.undo()
    assert "PermissionError" in result.metadata["cache_write_skipped"]
    assert _same(result, fresh)
    (target,) = _dirs(tmp_path)
    assert not (target / "manifest.json").exists()
    assert [p.name for p in target.iterdir()] == []  # no temps, lock released
    _rebuilds_equal_fresh(tmp_path, frame, fresh)


@pytest.mark.skipif(
    os.name != "nt", reason="POSIX replaces open files; the Windows failure mode cannot occur"
)
def test_reader_holding_npz_open_during_replacement_forces_retry_then_succeeds(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    """A reader holds features.npz open while the writer replaces it (Windows: PermissionError)."""
    _, manifest, npz = _published(tmp_path, frame)
    npz.write_bytes(b"corrupt")  # force a republish
    real_savez, real_replace = np.savez_compressed, os.replace
    state = {"holder": None, "attempts": 0}

    def savez_then_reader_opens(handle, **arrays):
        real_savez(handle, **arrays)
        state["holder"] = npz.open("rb")  # reader appears after the writer started, before replace

    def counting_replace(src, dst):
        if str(dst).endswith("features.npz"):
            state["attempts"] += 1
        try:
            return real_replace(src, dst)
        except PermissionError:
            state["holder"].close()  # deterministic: the reader goes away after the first failure
            raise

    monkeypatch.setattr(store_module.np, "savez_compressed", savez_then_reader_opens)
    monkeypatch.setattr(store_module.os, "replace", counting_replace)
    try:
        result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    finally:
        state["holder"].close()
        monkeypatch.undo()
    assert state["attempts"] >= 2  # the first replace really failed and was retried
    assert "cache_write_skipped" not in result.metadata
    assert _same(result, fresh)
    final = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert final.metadata["cache_hit"] is True and _same(final, fresh) and manifest.is_file()


@pytest.mark.skipif(os.name != "nt", reason="needs Windows sharing semantics")
def test_reader_open_without_retry_logic_would_skip_the_write(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    """Mutation check: with a single replace attempt the same scenario skips the cache write."""
    _, _, npz = _published(tmp_path, frame)
    npz.write_bytes(b"corrupt")
    real_savez = np.savez_compressed
    holder = {}

    def savez_then_reader_opens(handle, **arrays):
        real_savez(handle, **arrays)
        holder["f"] = npz.open("rb")

    monkeypatch.setattr(store_module.np, "savez_compressed", savez_then_reader_opens)
    monkeypatch.setattr(store_module, "_REPLACE_ATTEMPTS", 1)
    try:
        result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    finally:
        holder["f"].close()
    assert "PermissionError" in result.metadata["cache_write_skipped"]
    assert _same(result, fresh)


def test_tz_rule_file_digest_changes_key_and_sources_recorded(frame, monkeypatch) -> None:
    zones = store_module._key_components(frame, CONFIG)["timezone_rules"]["zones"]
    assert zones["Europe/Berlin"]["sources"] and zones["Europe/Berlin"]["sampled_offsets"]
    baseline = _key(frame)
    monkeypatch.setattr(
        store_module, "_tz_source_digests", lambda name: {"tzdata": "other-tzif-bytes"}
    )
    assert _key(frame) != baseline


def test_tz_source_digests_are_real_file_hashes() -> None:
    sources = store_module._tz_source_digests("Europe/Berlin")
    assert sources and all(len(v) == 64 for v in sources.values())
    assert store_module._tz_source_digests("Not/AZone") == {}
    assert store_module._tz_source_digests("../etc/passwd") == {}


def test_unlocatable_zone_makes_cache_unusable_no_read_no_write(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    FeatureStore.load_or_build(frame, CONFIG, tmp_path)  # a valid entry exists for the real key
    before = sorted(p.name for p in tmp_path.rglob("*"))
    monkeypatch.setattr(store_module, "_tz_source_digests", lambda name: {})
    fp = store_module._timezone_fingerprint(("Europe/Berlin",))
    assert fp["uncacheable"] == "UNCACHEABLE:tz_rules_unavailable:Europe/Berlin"
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    assert result.metadata["cache_hit"] is False and _same(result, fresh)
    assert (
        sorted(p.name for p in tmp_path.rglob("*")) == before
    )  # nothing read-through, nothing written


def test_research_speed_helpers_are_scanned_only_importgraph_is_exempt(tmp_path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "research_speed").mkdir()
    (src / "pkg" / "__init__.py").write_text("")
    (src / "research_speed" / "__init__.py").write_text("")
    (src / "pkg" / "entry.py").write_text("from research_speed import helper\n")
    (src / "research_speed" / "helper.py").write_text("X = 1\n")
    entries = (src / "pkg" / "entry.py",)
    assert not store_module._code_fingerprint(entries, src).startswith("UNCACHEABLE")
    (src / "research_speed" / "helper.py").write_text(
        "import importlib\nm = importlib.import_module('x')\n"
    )
    got = store_module._code_fingerprint(entries, src)
    assert got.startswith("UNCACHEABLE:dynamic_import:") and "research_speed/helper.py" in got


def test_importgraph_itself_is_exempt_but_hashed(tmp_path) -> None:
    src = tmp_path / "src"
    (src / "research_speed").mkdir(parents=True)
    (src / "research_speed" / "__init__.py").write_text("")
    graph = src / "research_speed" / "importgraph.py"
    graph.write_text("NAMES = ('import_module', '__import__')\n")
    (src / "entry.py").write_text("from research_speed import importgraph\n")
    first = store_module._code_fingerprint((src / "entry.py",), src)
    assert not first.startswith("UNCACHEABLE")
    graph.write_text("NAMES = ('import_module',)\n")
    assert store_module._code_fingerprint((src / "entry.py",), src) != first


def _race_on_lock(target: Path, workers: int = 6, hold: float = 0.05):
    inside = {"now": 0, "max": 0, "entered": 0}
    guard = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker() -> None:
        barrier.wait()
        with store_module._publish_lock(target) as locked:
            if not locked:
                return
            with guard:
                inside["now"] += 1
                inside["entered"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(hold)
            with guard:
                inside["now"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return inside


def test_contenders_racing_on_a_stale_lock_one_at_a_time_and_nobody_deletes_new_owner(
    tmp_path,
) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    lock = target / ".publish.lock"
    lock.write_text("dead-writer")
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))
    inside = _race_on_lock(target)
    assert inside["max"] == 1 and inside["entered"] == 6  # serialised, everyone got its turn
    assert not lock.exists()
    assert [p.name for p in target.iterdir()] == []  # stale lock removed, no leftovers


def _stale_lock(target: Path) -> Path:
    target.mkdir()
    lock = target / ".publish.lock"
    lock.write_text("dead-writer")
    old = lock.stat().st_mtime - 10_000
    os.utime(lock, (old, old))
    return lock


def test_takeover_restores_live_owner_created_between_check_and_rename(
    tmp_path, monkeypatch
) -> None:
    lock = _stale_lock(tmp_path / "entry")
    real_replace = os.replace
    seam = {"done": False}

    def live_owner_appears_then_replace(src, dst):
        if not seam["done"] and str(src).endswith(".publish.lock"):
            seam["done"] = True
            Path(src).unlink()
            Path(src).write_text("live-owner-token")  # brand-new lock (new inode, fresh mtime)
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", live_owner_appears_then_replace)
    assert store_module._takeover_stale_lock(lock) is True
    monkeypatch.undo()
    assert lock.read_text() == "live-owner-token"  # live owner's lock is back in place
    assert [p.name for p in lock.parent.iterdir()] == [".publish.lock"]  # no grave left
    # takeover abandoned: a fresh lock is simply waited for, not taken
    assert store_module._takeover_stale_lock(lock) is False


def test_takeover_drops_displaced_file_when_a_newer_lock_already_stands(
    tmp_path, monkeypatch
) -> None:
    lock = _stale_lock(tmp_path / "entry")
    real_replace = os.replace
    seam = {"done": False}

    def two_new_owners(src, dst):
        if not seam["done"] and str(src).endswith(".publish.lock"):
            seam["done"] = True
            Path(src).unlink()
            Path(src).write_text("live-owner-1")
            real_replace(src, dst)  # we move owner-1's lock away ...
            lock.write_text("owner-2")  # ... and owner-2 grabs the free name before the restore
            return None
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", two_new_owners)
    assert store_module._takeover_stale_lock(lock) is True
    monkeypatch.undo()
    assert lock.read_text() == "owner-2"  # never overwritten by the restore
    assert [p.name for p in lock.parent.iterdir()] == [".publish.lock"]


def test_takeover_rechecks_before_rename_and_skips_a_replaced_lock(tmp_path, monkeypatch) -> None:
    lock = _stale_lock(tmp_path / "entry")
    real_snapshot = store_module._lock_snapshot
    calls = {"n": 0}

    def snapshot_then_swap(path):
        result = real_snapshot(path)
        calls["n"] += 1
        if calls["n"] == 1:  # between the stale read and the re-check a new owner replaces it
            path.unlink()
            path.write_text("fresh-owner")
        return result

    monkeypatch.setattr(store_module, "_lock_snapshot", snapshot_then_swap)
    assert store_module._takeover_stale_lock(lock) is True
    monkeypatch.undo()
    assert lock.read_text() == "fresh-owner"  # never even renamed
    assert [p.name for p in lock.parent.iterdir()] == [".publish.lock"]


def test_takeover_of_a_truly_stale_lock_still_works(tmp_path) -> None:
    lock = _stale_lock(tmp_path / "entry")
    assert store_module._takeover_stale_lock(lock) is True
    assert list(lock.parent.iterdir()) == []


def test_lock_released_when_write_fails_and_interrupt_in_body(tmp_path) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    with pytest.raises(KeyboardInterrupt), store_module._publish_lock(target) as locked:
        assert locked
        raise KeyboardInterrupt
    assert not (target / ".publish.lock").exists()


def test_empty_lock_left_by_our_own_failed_token_write_is_released(tmp_path, monkeypatch) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    real_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        handle.write = lambda text: (_ for _ in ()).throw(OSError("disk full"))  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(store_module.os, "fdopen", failing_fdopen)
    with pytest.raises(OSError), store_module._publish_lock(target):
        pass
    monkeypatch.undo()
    assert not (target / ".publish.lock").exists()


def test_eight_threads_same_key_finish_fast_and_leave_no_lock(tmp_path, frame, fresh) -> None:
    workers = 8
    results: list = []
    errors: list = []
    barrier = threading.Barrier(workers)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(FeatureStore.load_or_build(frame, CONFIG, tmp_path))
        except BaseException as exc:
            errors.append(exc)

    started = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    assert not errors and len(results) == workers
    assert all(_same(r, fresh) for r in results)
    assert elapsed < 20, (
        f"publishers waited on a leaked lock ({elapsed:.1f}s)"
    )  # wait bound is 30 s
    assert all("cache_write_skipped" not in r.metadata for r in results)
    (target,) = _dirs(tmp_path)
    assert sorted(p.name for p in target.iterdir()) == ["features.npz", "manifest.json"]


def test_release_retries_unlink_on_permission_error(tmp_path, monkeypatch) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    real_unlink = Path.unlink
    failures = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self.name == ".publish.lock" and failures["n"] < 3:
            failures["n"] += 1
            raise PermissionError("held open by a poller")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with store_module._publish_lock(target) as locked:
        assert locked
    monkeypatch.undo()
    assert failures["n"] == 3
    assert not (target / ".publish.lock").exists()


def test_release_never_drops_a_lock_it_does_not_own_even_with_retries(tmp_path) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    lock = target / ".publish.lock"
    with store_module._publish_lock(target) as locked:
        assert locked
        lock.write_text("someone-else")  # simulates a takeover of our (stale) lock
    assert lock.read_text() == "someone-else"


def test_persistent_unlink_failure_is_reported_not_silent(
    tmp_path, frame, fresh, monkeypatch
) -> None:
    real_unlink = Path.unlink

    def stuck_unlink(self, *args, **kwargs):
        if self.name == ".publish.lock":
            raise PermissionError("held open forever")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stuck_unlink)
    monkeypatch.setattr(store_module, "_RELEASE_ATTEMPTS", 3)
    monkeypatch.setattr(store_module.time, "sleep", lambda s: None)
    result = FeatureStore.load_or_build(frame, CONFIG, tmp_path)
    monkeypatch.undo()
    (target,) = _dirs(tmp_path)
    assert result.metadata["publish_lock_leaked"] == str(target / ".publish.lock")
    assert _same(result, fresh) and "cache_write_skipped" not in result.metadata
    assert (target / ".publish.lock").exists()  # leaked on purpose of the injection
    # a later publisher is not blocked forever: the leaked lock goes stale and is taken over
    old = (target / ".publish.lock").stat().st_mtime - 10_000
    os.utime(target / ".publish.lock", (old, old))
    with store_module._publish_lock(target) as locked:
        assert locked
    assert not (target / ".publish.lock").exists()


def test_nested_publish_lock_same_thread_is_immediate_and_releases(tmp_path) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    started = time.monotonic()
    with store_module._publish_lock(target) as outer:
        assert outer
        with store_module._publish_lock(target) as inner:
            assert inner and not inner.leaked
            assert (target / ".publish.lock").exists()
        assert (target / ".publish.lock").exists()  # inner exit must not release the outer lock
    assert time.monotonic() - started < 1.0
    assert not (target / ".publish.lock").exists()
    # and the thread can lock again afterwards (depth bookkeeping reset)
    with store_module._publish_lock(target) as again:
        assert again
    assert not (target / ".publish.lock").exists()


def test_nested_lock_does_not_leak_into_other_threads(tmp_path) -> None:
    target = tmp_path / "entry"
    target.mkdir()
    seen = {}

    def other() -> None:
        with store_module._publish_lock(target) as locked:
            seen["locked"] = bool(locked)

    with store_module._publish_lock(target):
        thread = threading.Thread(target=other)
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()  # the other thread really waits for the owner
    thread.join(timeout=10)
    assert seen == {"locked": True}


def test_dir_lock_registry_is_evicted_when_idle(tmp_path) -> None:
    before = len(store_module._DIR_LOCKS)
    for index in range(25):
        target = tmp_path / f"entry{index}"
        target.mkdir()
        with store_module._publish_lock(target):
            pass
    assert len(store_module._DIR_LOCKS) == before
