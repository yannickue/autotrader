# ruff: noqa: E501
import pytest

from demo.learning import promotion
from demo.learning.promotion import PromotionThresholds, promotion_report

GOOD = dict(n_samples=500, net_expectancy_r=0.30, n_folds=5, chronological_validation=True, ece=0.04,
            brier=0.20, brier_base_rate=0.25, permutation_p=0.01, leakage_check_passed=True,
            max_market_share=0.4, max_session_share=0.5, max_family_share=0.45, max_drawdown_r=4.0,
            training_end_utc="2026-12-01T00:00:00+00:00")
CHAMP = dict(net_expectancy_r=0.05)
FWD = dict(phase="FROZEN", n_trades=150, net_expectancy_r=0.2, start_utc="2026-12-15T00:00:00+00:00")


def test_all_gates_pass_still_never_auto_promotes():
    r = promotion_report(GOOD, CHAMP, FWD)
    assert r["all_gates_passed"] and r["recommendation"] == "eligible_for_manual_review"
    assert r["auto_promote"] is False and r["advisory"] is True
    assert len(r["gates"]) == 10


def test_auto_promote_cannot_be_configured():
    assert promotion.AUTO_PROMOTE is False
    with pytest.raises(TypeError):
        PromotionThresholds(auto_promote=True)  # type: ignore[call-arg]
    assert promotion_report(GOOD, CHAMP, FWD, PromotionThresholds(min_samples=0))["auto_promote"] is False


def test_missing_evidence_fails_closed():
    r = promotion_report({}, None, None)
    assert not r["all_gates_passed"] and len(r["failed_gates"]) == 10
    assert r["auto_promote"] is False and r["recommendation"] == "not_eligible"


@pytest.mark.parametrize(
    ("key", "value", "gate"),
    [
        ("n_samples", 50, "enough_samples"),
        ("net_expectancy_r", -0.1, "positive_net_expectancy"),
        ("net_expectancy_r", 0.06, "better_than_champion"),
        ("chronological_validation", False, "chronological_validation"),
        ("n_folds", 1, "chronological_validation"),
        ("ece", 0.3, "calibration"),
        ("brier", 0.3, "calibration"),
        ("permutation_p", 0.4, "permutation_baseline"),
        ("leakage_check_passed", False, "no_leakage"),
        ("max_market_share", 0.95, "not_single_cluster"),
        ("max_session_share", None, "not_single_cluster"),
        ("max_drawdown_r", 20.0, "drawdown_acceptable"),
    ],
)
def test_each_gate_can_fail(key, value, gate):
    r = promotion_report({**GOOD, key: value}, CHAMP, FWD)
    assert gate in r["failed_gates"] and not r["all_gates_passed"] and r["auto_promote"] is False


@pytest.mark.parametrize(
    "fwd",
    [
        None,
        {**FWD, "phase": "DISCOVERY"},
        {**FWD, "n_trades": 10},
        {**FWD, "net_expectancy_r": -0.1},
        {**FWD, "start_utc": "2026-11-01T00:00:00+00:00"},  # overlaps training window
    ],
)
def test_forward_evidence_must_be_separate_frozen_and_positive(fwd):
    assert "separate_forward_evidence" in promotion_report(GOOD, CHAMP, fwd)["failed_gates"]


def test_better_than_champion_needs_champion():
    assert "better_than_champion" in promotion_report(GOOD, None, FWD)["failed_gates"]
