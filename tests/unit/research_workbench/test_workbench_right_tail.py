"""Right-tail measurement with non-finite rows (fast, pure numpy)."""

from __future__ import annotations

from research_workbench.entry_exit_adapter import right_tail


def test_right_tail_ignores_nonfinite_rows_with_explicit_counts() -> None:
    nan, inf = float("nan"), float("inf")
    tail = right_tail([3.0, nan, 5.0, inf, 0.5, -1.0], [3.5, 4.0, 6.0, 7.0, nan, 0.5])
    assert (tail["n_total"], tail["n_finite"], tail["n_excluded_nonfinite"]) == (6, 4, 2)
    assert tail["n_excluded_nonfinite_mfe"] == 1  # row 4: finite R, NaN MFE
    two, three, five = (tail["levels"][k] for k in ("2R", "3R", "5R"))
    assert two["p_realized_ge"] == 0.5 and two["n_p_basis"] == 4  # finite R = [3, 5, 0.5, -1]
    assert two["n_mfe_ge"] == 2 and two["mean_realized_given_mfe_ge"] == 4.0
    assert two["median_realized_given_mfe_ge"] == 4.0
    assert two["n_bucket_excluded_nonfinite"] == 2  # NaN-R row and Inf-R row both have MFE >= 2
    assert three["n_mfe_ge"] == 2 and three["mean_realized_given_mfe_ge"] == 4.0
    assert five["p_realized_ge"] == 0.25 and five["n_mfe_ge"] == 1
    assert five["mean_realized_given_mfe_ge"] == 5.0 and five["n_bucket_excluded_nonfinite"] == 1
    for level in tail["levels"].values():  # no NaN ever leaks into a statistic
        assert all(v is None or v == v for v in level.values() if not isinstance(v, str))


def test_right_tail_empty_or_all_nonfinite_is_explicit() -> None:
    for rows in ([], [float("nan"), float("inf")]):
        tail = right_tail(rows, [1.0] * len(rows))
        assert tail["n"] == 0 and tail["reason"] == "no_finite_rows"
        two = tail["levels"]["2R"]
        assert two["p_realized_ge"] is None and two["p_reason"] == "no_finite_rows"
        assert (
            two["mean_realized_given_mfe_ge"] is None and two["bucket_reason"] == "no_finite_rows"
        )


def test_right_tail_matches_hand_computation_on_clean_data() -> None:
    tail = right_tail([3.0, -1.0, 5.0, 0.5], [3.5, 0.5, 6.0, 2.5])
    two = tail["levels"]["2R"]
    assert tail["n_excluded_nonfinite"] == 0 and tail["n_total"] == tail["n_finite"] == 4
    assert two["p_realized_ge"] == 0.5 and two["n_mfe_ge"] == 3
    assert abs(two["mean_realized_given_mfe_ge"] - (3 + 5 + 0.5) / 3) < 1e-12
