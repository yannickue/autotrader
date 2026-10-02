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
    dag/<market>/<STAGE>/<key40>/manifest.json  commit marker, written LAST inside the directory
    experiments/<experiment_id>/<market>.json   run record (stage keys of the latest run)
    experiments/<experiment_id>/status/<market>__<STAGE>.json   PENDING | RUNNING | COMPLETE | FAILED

A lookup is a HIT only when the manifest is complete, names the requested stage/key/market and every payload file
matches its recorded size and sha256. Anything else (no manifest, partial/corrupt/changed file, unreadable JSON,
unprovable code closure) is a MISS with a reason; a lookup never raises and never returns stale data.

Publication is a directory TRANSACTION on a content-addressed key (same key => same content):

1. a COMPLETE, fully verified publication for the key already exists => ``publish`` is a no-op (a valid manifest is
   never deleted or rewritten);
2. otherwise payload + manifest (last) are written into a unique sibling ``<key40>.tmp.<uuid>`` (every file temp +
   fsync + os.replace), the temp directory is verified, and then renamed to ``<key40>``. An existing but INVALID target is
   first moved aside to ``<key40>.stale.<uuid>``; if the rename loses a race against another publisher whose valid
   publication appeared meanwhile, our temp directory is discarded. A crash/Ctrl+C at any point leaves either the previous
   state or the complete new one, never a half publication under ``<key40>``;
3. leftover ``.tmp.``/``.stale.`` directories older than ``LEFTOVER_MAX_AGE_S`` are removed defensively.

Status files (PENDING/RUNNING/COMPLETE/FAILED) are informational and never decide cache hits (only the verified manifest
does). They record the owner pid: after an interrupted run or an OS crash a ``RUNNING`` status whose owner process is
dead is reported as ``STALE`` (the stage is simply recomputed, nothing relies on the status being cleaned up).
No lock is needed for correctness (duplicate computation is only wasted work).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from alpha.fast.store import (
    CACHE_FORMAT_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SET_VERSION,
)
from research_speed import importgraph
from research_speed.artifact import config_hash
from research_speed.segments import Lookup, atomic_write_bytes, atomic_write_json, file_sha256

from .experiment import ExperimentSpec
from .stage_adapters import json_bytes, load_npz, npz_bytes

STAGES = ("FEATURES", "SIGNALS", "SIMULATION", "METRICS", "ENTRY_EXIT", "FIDELITY", "REPORT")
UNCACHEABLE = "UNCACHEABLE:"
_SRC_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = "manifest.json"
MANIFEST_SCHEMA = "research-workbench-artifact-1"
LEFTOVER_MAX_AGE_S = 30 * 60


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
ADAPTER_CODE = ("research_workbench/stage_adapters.py",)
DIFF_CODE = (
    "research_workbench/differential.py",
    "research_workbench/golden.py",
    "nautilus_kernel/replay_backtest.py",
)


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
    acode = code_hash(ADAPTER_CODE)
    cacheable = not any(c.startswith(UNCACHEABLE) for c in (fcode, scode, simcode, mcode, acode))
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
        "adapter_code_hash": acode,
    }
    signals = config_hash(comp["SIGNALS"])
    comp["SIMULATION"] = {
        "signals_key": signals,
        "cost_model": _plain(experiment.cost_model),
        "sizing": _plain(experiment.sizing),
        "rules": _plain(experiment.rules),
        "window": _plain(experiment.window),
        "sim_code_hash": simcode,
        "adapter_code_hash": acode,
    }
    simulation = config_hash(comp["SIMULATION"])
    comp["METRICS"] = {
        "simulation_key": simulation,
        "metric_version": metric_version,
        "split": experiment.split.to_dict(),
        "partitions_read": list(experiment.partitions_read),
        "gate": gate,
        "metric_code_hash": mcode,
        "adapter_code_hash": acode,
        "fastrun_digest": fastrun_digest,
    }
    metrics = config_hash(comp["METRICS"])
    return StageKeys(features, signals, simulation, metrics, comp, cacheable)


def entry_exit_key(simulation_key: str, config: dict[str, Any]) -> str:
    return config_hash(
        {"simulation_key": simulation_key, "config": config, "code": code_hash(ENTRY_EXIT_CODE)}
    )


def fidelity_key(
    signals_key: str,
    simulation_key: str,
    cost: Any,
    sizing: Any,
    rules: Any,
    window: Any,
    replay_config: dict[str, Any],
    nautilus_version: str,
    diff_code_hash: str,
) -> str:
    """FIDELITY: signals + simulation keys, the full execution config, the replay config actually used, the real
    nautilus version and the differential/golden/replay code closure hash."""
    return config_hash(
        {
            "signals_key": signals_key,
            "simulation_key": simulation_key,
            "cost": _plain(cost),
            "sizing": _plain(sizing),
            "rules": _plain(rules),
            "window": _plain(window),
            "replay_config": replay_config,
            "nautilus_version": nautilus_version,
            "diff_code_hash": diff_code_hash,
        }
    )


def report_key(keys: dict[str, str]) -> str:
    return config_hash(keys)


def _owner_alive(pid: Any) -> bool:
    from research_speed.runlock import pid_alive

    try:
        return pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


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
        return self._verify_dir(self.stage_dir(market, stage, key), market, stage, key)

    @staticmethod
    def _verify_dir(directory: Path, market: str, stage: str, key: str) -> Lookup:
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

    # -- publish (directory transaction) --
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
        final = self.stage_dir(market, stage, key)
        existing = self.lookup(market, stage, key)
        if existing.hit:  # content-addressed: a valid publication is never touched
            return existing.manifest or {}
        parent = final.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp = parent / f"{final.name}.tmp.{uuid.uuid4().hex[:12]}"
        tmp.mkdir()
        try:
            meta = {}
            for name, data in sorted(files.items()):
                atomic_write_bytes(tmp / name, data)
                meta[name] = {"size": len(data), "sha256": file_sha256(tmp / name)}
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
            atomic_write_json(
                tmp / _MANIFEST, manifest
            )  # commit marker LAST, inside the temp directory
            if not self._verify_dir(tmp, market, stage, key).hit:
                raise RuntimeError(f"{stage}/{key[:12]}: temp publication failed verification")
            winner = self._install(tmp, final, market, stage, key)
            return winner if winner is not None else manifest
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            self._cleanup_leftovers(parent, final.name)

    def _install(
        self, tmp: Path, final: Path, market: str, stage: str, key: str
    ) -> dict[str, Any] | None:
        """Rename ``tmp`` to ``final``. Returns the winner's manifest if another valid publication won the race."""
        parent = final.parent
        last_error: OSError | None = None
        for _ in range(5):
            if final.exists():
                current = self.lookup(market, stage, key)
                if current.hit:
                    return current.manifest or {}
                try:  # invalid target: move aside first, never delete in place
                    os.replace(final, parent / f"{final.name}.stale.{uuid.uuid4().hex[:12]}")
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.05)
                    continue
            try:
                os.rename(tmp, final)
                return None
            except OSError as exc:  # lost a race (target appeared) or transient sharing violation
                last_error = exc
                time.sleep(0.05)
        current = self.lookup(market, stage, key)
        if current.hit:
            return current.manifest or {}
        raise RuntimeError(f"could not install {final}: {last_error!r}")

    @staticmethod
    def _cleanup_leftovers(parent: Path, name: str) -> None:
        cutoff = time.time() - LEFTOVER_MAX_AGE_S
        try:
            for entry in parent.iterdir():
                if entry.name.startswith((f"{name}.tmp.", f"{name}.stale.")):
                    try:
                        if entry.stat().st_mtime < cutoff:
                            shutil.rmtree(entry, ignore_errors=True)
                    except OSError:
                        pass
        except OSError:
            pass

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
            "pid": os.getpid(),
            **info,
        }
        atomic_write_json(
            self.run_dir(experiment_id) / "status" / f"{market}__{stage}.json", payload
        )

    def read_status(self, experiment_id: str, market: str, stage: str) -> dict[str, Any] | None:
        path = self.run_dir(experiment_id) / "status" / f"{market}__{stage}.json"
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if status.get("status") == str(StageStatus.RUNNING) and not _owner_alive(status.get("pid")):
            return {
                **status,
                "status": "STALE",
                "was": "RUNNING",
            }  # owner died: never trust RUNNING
        return status

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


__all__ = (
    "ADAPTER_CODE",
    "DIFF_CODE",
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
