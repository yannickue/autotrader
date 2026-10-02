# ruff: noqa: E501
"""Artifact DAG of the research workbench (OFFLINE ONLY): stage keys + persistent, atomically published artifacts.

Stage keys (every key includes the previous stage key; any single changed input changes exactly the downstream keys)::

    FEATURES   dataset_hash + data_range + feature_config + feature_code_hash (import closure) + library versions
    SIGNALS    features_key + strategy_spec_hash + signal_code_hash
    SIMULATION signals_key + cost_model + sizing + rules + window + sim_code_hash
    METRICS    simulation_key + metric_version + split + partitions_read + gate + metric_code_hash
    ENTRY_EXIT simulation_key + diagnostic config + adapter code hash          (optional diagnostic stage)
    FIDELITY   signals_key + cost + replay config + nautilus version           (key only; written by ``compare``)
    REPORT     all of the above

Store layout below ``artifact_root``::

    features/<store_key>/...                    the existing ``FeatureStore`` cache (NOT duplicated)
    dag/<market>/<STAGE>/<key40>/<files>        compact npz/json payload
    dag/<market>/<STAGE>/<key40>/manifest.json  commit marker, written LAST (atomic, fsync'd)
    experiments/<experiment_id>/<market>.json   run record (stage keys of the latest run)
    experiments/<experiment_id>/status/<market>__<STAGE>.json   PENDING | RUNNING | COMPLETE | FAILED

A lookup is a HIT only when the manifest is complete, names the requested stage/key/market and every payload file
matches its recorded size and sha256. Anything else (no manifest, partial/corrupt/changed file, unreadable JSON,
unprovable code closure) is a MISS with a reason; a lookup never raises and never returns stale data.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np

from alpha.fast.store import (
    CACHE_FORMAT_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SET_VERSION,
)
from research_speed import importgraph
from research_speed.artifact import config_hash
from research_speed.segments import Lookup, atomic_write_bytes, atomic_write_json, file_sha256

from .experiment import ExperimentSpec

STAGES = ("FEATURES", "SIGNALS", "SIMULATION", "METRICS", "ENTRY_EXIT", "FIDELITY", "REPORT")
UNCACHEABLE = "UNCACHEABLE:"
_SRC_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = "manifest.json"
MANIFEST_SCHEMA = "research-workbench-artifact-1"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# ---- code hashes (import closure) ----
_CODE_CACHE: dict[tuple[str, ...], str] = {}


def clear_code_hash_cache() -> None:
    _CODE_CACHE.clear()


def code_hash(entries: tuple[str, ...]) -> str:
    """Hash of the import closure of ``entries`` (paths relative to ``src``); ``UNCACHEABLE:...`` if unprovable."""
    if entries in _CODE_CACHE:
        return _CODE_CACHE[entries]
    try:
        files = importgraph.closure([_SRC_ROOT / e for e in entries], [_SRC_ROOT])
        # same rule as ``FeatureStore._code_fingerprint``: only the analyser itself is exempt from the dynamic-import
        # scan (its detection tables name the loader functions); it stays in the closure so its edits change the key.
        scanned = [
            f
            for f in files
            if not (f.name == "importgraph.py" and f.parent.name == "research_speed")
        ]
        dynamic = importgraph.dynamic_import_files(scanned)
        if dynamic:
            names = ",".join(sorted(f.relative_to(_SRC_ROOT).as_posix() for f in dynamic))
            result = f"{UNCACHEABLE}dynamic_import:{names}"
        else:
            result = importgraph.hash_files(files, _SRC_ROOT)
    except Exception as exc:  # cannot prove => never cache
        result = f"{UNCACHEABLE}closure_error:{type(exc).__name__}"
    _CODE_CACHE[entries] = result
    return result


FEATURE_CODE = ("alpha/fast/store.py", "alpha/fast/__init__.py")
SIGNAL_CODE = ("alpha/fast/spec.py",)
SIM_CODE = ("alpha/fast/sim.py",)
METRIC_CODE = ("alpha/fast/screen.py",)
ENTRY_EXIT_CODE = ("research_workbench/entry_exit_adapter.py",)


def _library_versions() -> dict[str, str]:
    from importlib import metadata

    versions = {}
    for name in ("ta-lib", "numpy", "pandas", "numba"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
    return versions


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


@dataclass(frozen=True)
class StageKeys:
    """Per-stage fingerprints of one market of one experiment plus their human-readable components."""

    features: str
    signals: str
    simulation: str
    metrics: str
    components: dict[str, dict[str, Any]]
    cacheable: bool

    def key(self, stage: str) -> str:
        return {
            "FEATURES": self.features,
            "SIGNALS": self.signals,
            "SIMULATION": self.simulation,
            "METRICS": self.metrics,
        }[stage]

    def as_dict(self) -> dict[str, str]:
        return {
            "FEATURES": self.features,
            "SIGNALS": self.signals,
            "SIMULATION": self.simulation,
            "METRICS": self.metrics,
        }


def compute_keys(
    experiment: ExperimentSpec,
    market: str,
    dataset_hash: str,
    *,
    metric_version: str,
    gate: dict[str, Any],
    fastrun_digest: str,
) -> StageKeys:
    fcode = code_hash(FEATURE_CODE)
    scode = code_hash(SIGNAL_CODE)
    simcode = code_hash(SIM_CODE)
    mcode = code_hash(METRIC_CODE)
    cacheable = not any(c.startswith(UNCACHEABLE) for c in (fcode, scode, simcode, mcode))
    comp: dict[str, dict[str, Any]] = {}
    comp["FEATURES"] = {
        "market": market,
        "dataset_hash": dataset_hash,
        "data_range": [
            experiment.date_range[0] if experiment.date_range else None,
            experiment.last_read_date(),
        ],
        "feature_config": _plain(experiment.feature_config),
        "feature_code_hash": fcode,
        "library_versions": _library_versions(),
        "versions": [FEATURE_SCHEMA_VERSION, CACHE_FORMAT_VERSION, FEATURE_SET_VERSION],
    }
    features = config_hash(comp["FEATURES"])
    comp["SIGNALS"] = {
        "features_key": features,
        "strategy_spec_hash": experiment.strategy_spec.spec_hash(),
        "signal_code_hash": scode,
    }
    signals = config_hash(comp["SIGNALS"])
    comp["SIMULATION"] = {
        "signals_key": signals,
        "cost_model": _plain(experiment.cost_model),
        "sizing": _plain(experiment.sizing),
        "rules": _plain(experiment.rules),
        "window": _plain(experiment.window),
        "sim_code_hash": simcode,
    }
    simulation = config_hash(comp["SIMULATION"])
    comp["METRICS"] = {
        "simulation_key": simulation,
        "metric_version": metric_version,
        "split": experiment.split.to_dict(),
        "partitions_read": list(experiment.partitions_read),
        "gate": gate,
        "metric_code_hash": mcode,
        "fastrun_digest": fastrun_digest,
    }
    metrics = config_hash(comp["METRICS"])
    return StageKeys(features, signals, simulation, metrics, comp, cacheable)


def entry_exit_key(simulation_key: str, config: dict[str, Any]) -> str:
    return config_hash(
        {"simulation_key": simulation_key, "config": config, "code": code_hash(ENTRY_EXIT_CODE)}
    )


def fidelity_key(signals_key: str, cost: Any, replay_config: dict[str, Any], nautilus: str) -> str:
    return config_hash(
        {
            "signals_key": signals_key,
            "cost": _plain(cost),
            "replay_config": replay_config,
            "nautilus_version": nautilus,
        }
    )


def report_key(keys: dict[str, str]) -> str:
    return config_hash(keys)


# ---- serialisation helpers ----
def npz_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def load_npz(path: Path) -> dict[str, np.ndarray]:
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=1, default=str, sort_keys=True).encode("utf-8")


# ---- store ----
class ArtifactStore:
    """Persistent, content-addressed (by stage key) artifact store with atomic publish and verified load."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- paths --
    def stage_dir(self, market: str, stage: str, key: str) -> Path:
        return self.root / "dag" / market / stage / key[:40]

    def feature_cache_dir(self) -> Path:
        return self.root / "features"

    def run_dir(self, experiment_id: str) -> Path:
        return self.root / "experiments" / experiment_id

    # -- lookup --
    def lookup(self, market: str, stage: str, key: str, *, cacheable: bool = True) -> Lookup:
        if not cacheable:
            return Lookup(False, "UNCACHEABLE")
        try:
            return self._lookup(market, stage, key)
        except Exception as exc:  # corrupt / unreadable is a MISS, never an error
            return Lookup(False, f"UNREADABLE:{type(exc).__name__}")

    def _lookup(self, market: str, stage: str, key: str) -> Lookup:
        directory = self.stage_dir(market, stage, key)
        path = directory / _MANIFEST
        if not path.is_file():
            return Lookup(False, "NO_MANIFEST")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("complete") is not True:
            return Lookup(False, "NOT_COMPLETE")
        if (
            manifest.get("schema") != MANIFEST_SCHEMA
            or manifest.get("stage") != stage
            or manifest.get("market") != market
            or manifest.get("fingerprint") != key
        ):
            return Lookup(False, "FINGERPRINT_MISMATCH")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return Lookup(False, "NOT_COMPLETE")
        for name, meta in files.items():
            file = directory / name
            if not file.is_file():
                return Lookup(False, f"FILE_MISSING:{name}")
            if file.stat().st_size != meta["size"] or file_sha256(file) != meta["sha256"]:
                return Lookup(False, f"FILE_CHANGED:{name}")
        return Lookup(True, "HIT", key, manifest)

    # -- publish --
    def publish(
        self,
        market: str,
        stage: str,
        key: str,
        files: dict[str, bytes],
        *,
        experiment_id: str,
        code: str,
        runtime_s: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically publish payload files, then the manifest (commit marker) LAST."""
        directory = self.stage_dir(market, stage, key)
        (directory / _MANIFEST).unlink(missing_ok=True)  # no stale marker over fresh files
        meta = {}
        for name, data in sorted(files.items()):
            atomic_write_bytes(directory / name, data)
            meta[name] = {"size": len(data), "sha256": file_sha256(directory / name)}
        primary = sorted(files)[0]
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "experiment_id": experiment_id,
            "market": market,
            "stage": stage,
            "fingerprint": key,
            "artifact_sha256": meta[primary]["sha256"],
            "files": meta,
            "code_hash": code,
            "runtime_s": round(runtime_s, 4),
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "complete": True,
            **(extra or {}),
        }
        atomic_write_json(directory / _MANIFEST, manifest)
        return manifest

    # -- status / run records --
    def set_status(
        self, experiment_id: str, market: str, stage: str, status: StageStatus, **info: Any
    ) -> None:
        payload = {
            "experiment_id": experiment_id,
            "market": market,
            "stage": stage,
            "status": str(status),
            "updated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            **info,
        }
        atomic_write_json(
            self.run_dir(experiment_id) / "status" / f"{market}__{stage}.json", payload
        )

    def read_status(self, experiment_id: str, market: str, stage: str) -> dict[str, Any] | None:
        path = self.run_dir(experiment_id) / "status" / f"{market}__{stage}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_run_record(self, experiment_id: str, market: str, record: dict[str, Any]) -> None:
        atomic_write_json(self.run_dir(experiment_id) / f"{market}.json", record)

    def read_run_record(self, experiment_id: str, market: str) -> dict[str, Any] | None:
        try:
            return json.loads((self.run_dir(experiment_id) / f"{market}.json").read_text("utf-8"))
        except (OSError, ValueError):
            return None

    # -- one stage computation with status handling --
    def compute_and_publish(
        self,
        experiment_id: str,
        market: str,
        stage: str,
        key: str,
        compute: Any,
        *,
        code: str,
        cacheable: bool = True,
    ) -> tuple[Any, float, dict[str, Any] | None]:
        """Run ``compute() -> (files, extra, value)``; status RUNNING -> COMPLETE (after the manifest) | FAILED.

        A ``KeyboardInterrupt``/crash before the manifest leaves FAILED status and no COMPLETE manifest."""
        self.set_status(experiment_id, market, stage, StageStatus.RUNNING, fingerprint=key)
        started = time.perf_counter()
        try:
            files, extra, value = compute()
            runtime = time.perf_counter() - started
            manifest = None
            if cacheable:
                manifest = self.publish(
                    market,
                    stage,
                    key,
                    files,
                    experiment_id=experiment_id,
                    code=code,
                    runtime_s=runtime,
                    extra=extra,
                )
        except BaseException as exc:
            self.set_status(
                experiment_id,
                market,
                stage,
                StageStatus.FAILED,
                fingerprint=key,
                error=f"{type(exc).__name__}: {exc}",
                interrupted=isinstance(exc, KeyboardInterrupt),
            )
            raise
        self.set_status(
            experiment_id, market, stage, StageStatus.COMPLETE, fingerprint=key, runtime_s=runtime
        )
        return value, runtime, manifest


def new_tmp_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


__all__ = (
    "STAGES",
    "UNCACHEABLE",
    "ArtifactStore",
    "Lookup",
    "StageKeys",
    "StageStatus",
    "clear_code_hash_cache",
    "code_hash",
    "compute_keys",
    "entry_exit_key",
    "fidelity_key",
    "json_bytes",
    "load_npz",
    "npz_bytes",
    "report_key",
)
