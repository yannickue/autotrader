# ruff: noqa: E501
import pytest

from demo.learning.tracking import (
    LearningTracker,
    NonLocalTrackingURIError,
    assert_local_tracking_uri,
    git_commit,
    ui_command,
)


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:5000",
        "https://mlflow.example.com",
        "databricks",
        "postgresql://u:p@db/mlflow",
        "mysql://db/x",
        "s3://bucket/x",
        "file://remotehost/share/x",
        "sqlite://remotehost/x.db",
    ],
)
def test_non_local_uris_rejected(uri, tmp_path):
    with pytest.raises(NonLocalTrackingURIError):
        assert_local_tracking_uri(uri, tmp_path)


def test_local_uri_outside_root_rejected_and_inside_accepted(tmp_path):
    root = tmp_path / "mlflow"
    root.mkdir()
    with pytest.raises(NonLocalTrackingURIError):
        assert_local_tracking_uri(tmp_path / "elsewhere" / "mlflow.db", root)
    with pytest.raises(NonLocalTrackingURIError):
        assert_local_tracking_uri(str(root / ".." / "x.db"), root)
    assert assert_local_tracking_uri(root / "mlflow.db", root).startswith("file:")
    assert assert_local_tracking_uri("sqlite:///" + (root / "m.db").as_posix(), root).startswith("sqlite:///")
    assert assert_local_tracking_uri((root / "x").as_uri(), root).startswith("file:")


def test_tracker_refuses_remote_uri_and_ignores_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://evil.example.com")
    with pytest.raises(NonLocalTrackingURIError):
        LearningTracker("http://evil.example.com", root=tmp_path)
    t = LearningTracker(root=tmp_path / "mlflow")  # env var is never consulted
    assert t.uri.startswith("sqlite:///") and "evil" not in t.uri


def test_ui_is_localhost_only():
    cmd = ui_command()
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"


@pytest.fixture(scope="module")
def tracker(tmp_path_factory):
    return LearningTracker(root=tmp_path_factory.mktemp("mlf") / "mlflow", commit="deadbeef")


def test_lineage_tags_params_metrics_and_artifacts(tracker, tmp_path):
    art = tmp_path / "model.txt"
    art.write_text("x")
    window = {"start_utc": "2026-10-01T00:00:00+00:00", "end_utc": "2026-11-01T00:00:00+00:00", "n": 120}
    rid = tracker.log_run(model_name="lgbm_batch", model_version="v0001", feature_version="demo-features-1",
                          window=window, params={"seed": 0}, metrics={"auc": 0.61, "bad": float("nan")},
                          artifacts=[art])
    tags = tracker.get_tags(rid)
    assert tags["role"] == "challenger" and tags["shadow_only"] == "true"
    assert tags["git_commit"] == "deadbeef" and tags["feature_version"] == "demo-features-1"
    assert tags["window_start_utc"] == window["start_utc"] and tags["window_n"] == "120"
    run = tracker.client.get_run(rid)
    assert run.data.params["feature_version"] == "demo-features-1" and run.data.params["seed"] == "0"
    assert run.data.metrics == {"auc": 0.61}
    assert [a.path for a in tracker.client.list_artifacts(rid)] == ["model.txt"]
    assert tracker.latest_run("lgbm_batch", "challenger") == rid


def test_secret_looking_keys_refused(tracker):
    w = {"start_utc": "a", "end_utc": "b", "n": 1}
    for k in ("api_key", "MLFLOW_TRACKING_PASSWORD", "auth_token"):
        with pytest.raises(ValueError, match="secret"):
            tracker.log_run(model_name="m", model_version="v", feature_version="f", window=w, params={k: "x"})


def test_role_change_is_manual_and_recorded(tracker):
    w = {"start_utc": "a", "end_utc": "b", "n": 1}
    rid = tracker.log_run(model_name="m2", model_version="v1", feature_version="f", window=w)
    with pytest.raises(ValueError, match="approved_by"):
        tracker.record_role_change(run_id=rid, to_role="champion", approved_by=" ", promotion_report={})
    with pytest.raises(ValueError, match="auto_promote"):
        tracker.record_role_change(run_id=rid, to_role="champion", approved_by="owner",
                                   promotion_report={"auto_promote": True})
    assert tracker.get_tags(rid)["role"] == "challenger"
    tracker.record_role_change(run_id=rid, to_role="champion", approved_by="owner",
                               promotion_report={"auto_promote": False, "failed_gates": []})
    assert tracker.get_tags(rid)["role"] == "champion"
    hist = tracker.promotion_history()
    assert hist[-1]["approved_by"] == "owner" and hist[-1]["from_role"] == "challenger"
    with pytest.raises(ValueError):
        tracker.log_run(model_name="m", model_version="v", feature_version="f", window=w, role="boss")


def test_git_commit_helper_returns_string():
    assert isinstance(git_commit(), str) and git_commit()
