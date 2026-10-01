"""FeatureStore cache safety: atomic publish, verified load (defect = MISS), code fingerprint."""

from __future__ import annotations

import json
import os
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
    with pytest.raises(OSError):
        FeatureStore.load_or_build(frame, CONFIG, tmp_path)
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
    with pytest.raises(OSError):
        FeatureStore.load_or_build(frame, CONFIG, tmp_path)
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
    with pytest.raises(OSError):
        FeatureStore.load_or_build(frame, CONFIG, tmp_path)
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


def test_real_fingerprint_follows_closure_content_not_unrelated_files(frame, monkeypatch) -> None:
    src_root = Path(store_module.__file__).resolve().parents[2]
    baseline = _key(frame)
    real_digest = importgraph.content_digest
    in_closure = (src_root / "alpha" / "session.py").resolve()
    unrelated = (src_root / "risk" / "__init__.py").resolve()
    assert unrelated.is_file() and unrelated not in importgraph.closure(
        [Path(store_module.__file__).resolve()], [src_root]
    )

    def patched(target: Path) -> str:
        if target.resolve() == in_closure:
            return "edited"
        if target.resolve() == unrelated:
            return "edited-unrelated"
        return real_digest(target)

    monkeypatch.setattr(importgraph, "content_digest", patched)
    # an edit of a closure module (alpha.session) changes the key ...
    assert _key(frame) != baseline
    # ... and an edit of a file outside the closure does not
    monkeypatch.setattr(
        importgraph,
        "content_digest",
        lambda t: "edited-unrelated" if t.resolve() == unrelated else real_digest(t),
    )
    assert _key(frame) == baseline


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
