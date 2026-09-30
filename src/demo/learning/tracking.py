# ruff: noqa: E501
"""LOCAL-ONLY MLflow experiment tracking + champion/challenger tags + promotion history.

  * The tracking/registry URI must be a local path, `file:` or `sqlite:` URI located UNDER the allowed
    root (default `<repo>/artifacts/mlflow`). Any other scheme (http, https, databricks, postgres,
    s3, ...) or a path outside the root raises `NonLocalTrackingURIError`. The environment variable
    `MLFLOW_TRACKING_URI` is never consulted (an explicit `MlflowClient` is used).
  * Nothing from the environment is logged; parameter/tag keys that look like secrets are refused.
  * `role=champion|challenger` is a run tag. The champion of the shadow phase is the STATIC demo
    policy; setting a role is a MANUAL record (`approved_by` required) and nothing in the trading
    path reads it. There is no automatic promotion (see `promotion.py`).
  * If the MLflow UI is ever launched it must be bound to 127.0.0.1 (`ui_command`).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

EXPERIMENT = "demo-learning"
PROMOTION_EXPERIMENT = "demo-promotion-history"
ROLES = ("champion", "challenger")
_SECRET_RE = re.compile(r"(secret|password|passwd|token|api[_-]?key|credential|private|auth|env)", re.I)
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "artifacts" / "mlflow"


class NonLocalTrackingURIError(ValueError):
    pass


def _uri_to_path(uri: str) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", uri):
        return Path(uri)
    p = urlparse(uri)
    if p.scheme == "":
        return Path(uri)
    if p.scheme in ("file", "sqlite"):
        if p.netloc not in ("", "localhost"):
            raise NonLocalTrackingURIError(f"non-local host in tracking URI: {p.netloc!r}")
        path = unquote(p.path)
        if re.match(r"^/[A-Za-z]:", path):  # file:///C:/x  /  sqlite:///C:/x
            path = path[1:]
        if not path:
            raise NonLocalTrackingURIError("empty path in tracking URI")
        return Path(path)
    raise NonLocalTrackingURIError(f"tracking URI scheme {p.scheme!r} is not local")


def assert_local_tracking_uri(uri: str | Path, root: Path | None = None) -> str:
    """Return the canonical local URI or raise `NonLocalTrackingURIError`."""
    root = (root or DEFAULT_ROOT).resolve()
    s = str(uri)
    path = _uri_to_path(s).resolve()
    if not path.is_relative_to(root):
        raise NonLocalTrackingURIError(f"tracking path {path} is outside the allowed root {root}")
    if s.startswith("sqlite:"):
        return "sqlite:///" + path.as_posix()
    return path.as_uri()


def git_commit() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                           cwd=Path(__file__).resolve().parent, check=False)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _check_no_secrets(d: dict[str, Any]) -> None:
    for k in d:
        if _SECRET_RE.search(str(k)):
            raise ValueError(f"refusing to log secret-looking key {k!r}")


def _finite_metrics(m: dict[str, Any]) -> dict[str, float]:
    return {k: float(v) for k, v in m.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def ui_command(root: Path | None = None, port: int = 5000) -> list[str]:
    """Command to launch the MLflow UI bound to localhost only (not executed here)."""
    uri = assert_local_tracking_uri((root or DEFAULT_ROOT) / "mlflow.db", root)
    return ["mlflow", "ui", "--backend-store-uri", uri, "--host", "127.0.0.1", "--port", str(port)]


class LearningTracker:
    def __init__(self, tracking_uri: str | Path | None = None, *, root: Path | None = None,
                 commit: str | None = None) -> None:
        from mlflow import MlflowClient

        self.root = (root or DEFAULT_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        default = "sqlite:///" + (self.root / "mlflow.db").as_posix()
        self.uri = assert_local_tracking_uri(tracking_uri or default, self.root)
        self.commit = commit or git_commit()
        (self.root / "artifacts").mkdir(exist_ok=True)
        self.client = MlflowClient(tracking_uri=self.uri, registry_uri=self.uri)

    def _experiment(self, name: str) -> str:
        exp = self.client.get_experiment_by_name(name)
        if exp is not None:
            return exp.experiment_id
        return self.client.create_experiment(name, artifact_location=(self.root / "artifacts" / name).as_uri())

    def log_run(
        self,
        *,
        model_name: str,
        model_version: str,
        feature_version: str,
        window: dict[str, Any],
        params: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        artifacts: list[Path] | None = None,
        role: str = "challenger",
        extra_tags: dict[str, str] | None = None,
    ) -> str:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        params = dict(params or {})
        _check_no_secrets(params)
        _check_no_secrets(extra_tags or {})
        tags = {
            "role": role, "model_name": model_name, "model_version": model_version,
            "feature_version": feature_version, "git_commit": self.commit,
            "window_start_utc": str(window.get("start_utc")), "window_end_utc": str(window.get("end_utc")),
            "window_n": str(window.get("n")), "shadow_only": "true", **(extra_tags or {}),
        }
        run = self.client.create_run(self._experiment(EXPERIMENT), tags=tags,
                                     run_name=f"{model_name}-{model_version}")
        rid = run.info.run_id
        allp = {"feature_version": feature_version, "git_commit": self.commit,
                "window_start_utc": window.get("start_utc"), "window_end_utc": window.get("end_utc"),
                "window_n": window.get("n"), **params}
        for k, v in allp.items():
            self.client.log_param(rid, k, str(v)[:500])
        for k, v in _finite_metrics(metrics or {}).items():
            self.client.log_metric(rid, k, v)
        for a in artifacts or []:
            self.client.log_artifact(rid, str(a))
        self.client.set_terminated(rid, "FINISHED")
        return rid

    def get_tags(self, run_id: str) -> dict[str, str]:
        return dict(self.client.get_run(run_id).data.tags)

    def latest_run(self, model_name: str, role: str | None = None) -> str | None:
        filt = f"tags.model_name = '{model_name}'" + (f" and tags.role = '{role}'" if role else "")
        runs = self.client.search_runs([self._experiment(EXPERIMENT)], filter_string=filt,
                                       order_by=["attributes.start_time DESC"], max_results=1)
        return runs[0].info.run_id if runs else None

    def record_role_change(self, *, run_id: str, to_role: str, approved_by: str,
                           promotion_report: dict[str, Any]) -> str:
        """MANUAL record of a role change (promotion history). Requires a named human approver and
        refuses reports claiming automatic promotion. Returns the history run id."""
        if to_role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by (a human owner) is required; there is no automatic promotion")
        if promotion_report.get("auto_promote"):
            raise ValueError("promotion report claims auto_promote; refused")
        prev = self.get_tags(run_id).get("role", "challenger")
        hist = self.client.create_run(
            self._experiment(PROMOTION_EXPERIMENT),
            tags={"source_run_id": run_id, "from_role": prev, "to_role": to_role,
                  "approved_by": approved_by, "git_commit": self.commit, "manual": "true"},
            run_name=f"role-{prev}-to-{to_role}",
        )
        self.client.log_dict(hist.info.run_id, json.loads(json.dumps(promotion_report, default=str)),
                             "promotion_report.json")
        self.client.set_terminated(hist.info.run_id, "FINISHED")
        self.client.set_tag(run_id, "role", to_role)
        return hist.info.run_id

    def promotion_history(self) -> list[dict[str, str]]:
        runs = self.client.search_runs([self._experiment(PROMOTION_EXPERIMENT)],
                                       order_by=["attributes.start_time ASC"], max_results=1000)
        return [dict(r.data.tags) for r in runs]

    def close(self) -> None:  # symmetry; the client holds no long-lived resources
        return None
